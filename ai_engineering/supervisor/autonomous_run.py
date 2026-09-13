import json
import hashlib
import uuid
import datetime
import asyncio
import inspect
import time
from enum import Enum
from dataclasses import dataclass, field, replace
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
from ai_engineering.supervisor.pr_provider import (
    PullRequestIdentity,
    PullRequestReceipt,
    MergeReceipt,
    PullRequestProvider,
    compute_pr_receipt_digest,
    compute_merge_receipt_digest,
    PULL_REQUEST_IDENTITY_SCHEMA_VERSION,
    PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
    MERGE_RECEIPT_SCHEMA_VERSION,
)

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
    allowed_scope: tuple[str, ...] = ()
    required_validators: tuple[str, ...] = ()
    success_criteria: str = ""
    reasoning_summary: str = ""

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
    astra_receipts: list[str] = field(default_factory=list)
    ci_receipts: list[str] = field(default_factory=list)
    pr_receipts: list[str] = field(default_factory=list)
    merge_receipts: list[str] = field(default_factory=list)
    pr_number: int | None = None
    merged_commit_sha: str | None = None


class AstraProposalProvider:
    async def request_proposal(self, run_id: str, state: SupervisorState, **kwargs: Any) -> AstraNextActionProposal:
        raise NotImplementedError

class ScriptedAstraProposalProvider(AstraProposalProvider):
    """Scripted Astra proposal provider yielding sequence of actions or default IMPLEMENT then STOP_SUCCESS."""

    def __init__(
        self,
        target_worker: str = "codex",
        target_capability: str = "code",
        expected_effect_class: str = "REPOSITORY_WRITE",
        expected_stop_boundary: str = "LOCAL_DIFF",
        scripted_actions: list[NextActionType] | None = None,
    ) -> None:
        self.target_worker = target_worker
        self.target_capability = target_capability
        self.expected_effect_class = expected_effect_class
        self.expected_stop_boundary = expected_stop_boundary
        self.scripted_actions = list(scripted_actions) if scripted_actions is not None else None
        self.step = 0

    async def request_proposal(self, run_id: str, state: SupervisorState, **kwargs: Any) -> AstraNextActionProposal:
        if self.scripted_actions is not None:
            if self.step < len(self.scripted_actions):
                act = self.scripted_actions[self.step]
                self.step += 1
            else:
                act = NextActionType.STOP_SUCCESS
            act_type = NextActionType(act) if not isinstance(act, NextActionType) else act

            eff = "READ_ONLY"
            sb = "READ_ONLY"
            if act_type == NextActionType.IMPLEMENT:
                eff = self.expected_effect_class
                sb = self.expected_stop_boundary
            elif act_type == NextActionType.CREATE_PR:
                eff = "PR_MUTATION"
                sb = "READY_PR"
            elif act_type == NextActionType.WAIT_FOR_CI:
                eff = "READ_ONLY"
                sb = "READY_PR"
            elif act_type == NextActionType.MERGE_IF_GREEN:
                eff = "PR_MERGE"
                sb = "MERGE"

            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id=f"prop-scripted-{self.step}-{run_id}",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=act_type,
                objective=f"Scripted step {self.step}: {act_type.value}",
                recommended_capability=self.target_capability if act_type == NextActionType.IMPLEMENT else "none",
                recommended_worker=self.target_worker if act_type == NextActionType.IMPLEMENT else "none",
                expected_effect_class=eff,
                expected_stop_boundary=sb,
                allowed_scope=(),
                required_validators=(),
                success_criteria=f"Execution of {act_type.value}",
                reasoning_summary=f"Scripted action {act_type.value}",
            )

        if state.latest_verified_result_id:
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id=f"prop-stop-{run_id}",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=NextActionType.STOP_SUCCESS,
                objective="VerifiedResult PASS evidenced, terminating run successfully.",
                recommended_capability="none",
                recommended_worker="none",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(),
                required_validators=(),
                success_criteria="",
                reasoning_summary="Autonomous task implementation evidenced PASS",
            )

        self.step += 1
        return AstraNextActionProposal(
            schema_version="hermes.astra-next-action.v1",
            proposal_id=f"prop-impl-{self.step}",
            run_id=run_id,
            task_id=state.current_task_id,
            action_type=NextActionType.IMPLEMENT,
            objective="Implement bounded task intent",
            recommended_capability=self.target_capability,
            recommended_worker=self.target_worker,
            expected_effect_class=self.expected_effect_class,
            expected_stop_boundary=self.expected_stop_boundary,
            allowed_scope=(),
            required_validators=(),
            success_criteria="Verification PASS",
            reasoning_summary="Autonomous IMPLEMENT action dispatched",
        )

class CIStatusProvider:
    async def wait_for_ci(self, run_id: str, sha: str, **kwargs: Any) -> Any:
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
        evidence_root: str = "/tmp",
        provider_mode: str = "real",
        pr_provider: PullRequestProvider | None = None,
        allow_pr_create: bool = False,
        allow_pr_merge: bool = False,
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
        self.provider_mode = provider_mode
        self.pr_provider = pr_provider
        self.allow_pr_create = allow_pr_create
        self.allow_pr_merge = allow_pr_merge

        events = self.store.load_events(self.run_id)
        from ai_engineering.supervisor.events import SupervisorEventType

        proposals_count = 0
        decisions_count = 0
        self.child_tasks = 0
        self.retries = 0
        self.fix_cycles = 0
        self.consecutive_failures = 0
        self.provider_calls = 0

        self.policy_receipts: list[str] = []
        self.routing_receipts: list[str] = []
        self.verified_results: list[str] = []
        self.astra_receipts: list[str] = []
        self.ci_receipts: list[str] = []
        self.pr_receipts: list[str] = []
        self.merge_receipts: list[str] = []
        self.pending_ci_sha: str | None = None
        self.active_pr: PullRequestIdentity | None = None
        self.active_pr_number: int | None = None
        self.active_pr_head_sha: str | None = None
        self.merged_commit_sha: str | None = None

        for ev in events:
            ev_type = getattr(ev.event_type, "value", str(ev.event_type))
            if ev_type == "ASTRA_PROPOSAL_CREATED":
                self.provider_calls += 1
                proposals_count += 1
                if ev.payload and ev.payload.get("receipt_id"):
                    self.astra_receipts.append(ev.payload["receipt_id"])
            elif ev_type == "CI_WAIT_STARTED":
                if ev.payload and ev.payload.get("sha"):
                    self.pending_ci_sha = ev.payload["sha"]
            elif ev_type in ("CI_GREEN", "CI_WAIT_TIMEOUT", "CI_FAILED"):
                self.pending_ci_sha = None
            elif ev_type == "CI_STATUS_OBSERVED":
                if ev.payload and ev.payload.get("receipt_id"):
                    self.ci_receipts.append(ev.payload["receipt_id"])
                if self.pending_ci_sha is None and ev.payload:
                    obs_sha = ev.payload.get("exact_sha") or ev.payload.get("sha")
                    if obs_sha:
                        self.pending_ci_sha = obs_sha
            elif ev_type in ("PR_CREATED", "PR_RECOVERED"):
                if ev.payload:
                    rcpt_id = ev.payload.get("receipt_id")
                    if rcpt_id and rcpt_id not in self.pr_receipts:
                        self.pr_receipts.append(rcpt_id)
                    if ev.payload.get("pr_number"):
                        self.active_pr_number = ev.payload["pr_number"]
                    if ev.payload.get("head_sha"):
                        self.active_pr_head_sha = ev.payload["head_sha"]
            elif ev_type in ("PR_MERGED", "PR_MERGE_RECOVERED"):
                if ev.payload:
                    rcpt_id = ev.payload.get("receipt_id")
                    if rcpt_id and rcpt_id not in self.merge_receipts:
                        self.merge_receipts.append(rcpt_id)
                    if ev.payload.get("merged_commit_sha"):
                        self.merged_commit_sha = ev.payload["merged_commit_sha"]
            elif ev_type == "PR_HEAD_CHANGED":
                if ev.payload and ev.payload.get("new_head_sha"):
                    self.active_pr_head_sha = ev.payload["new_head_sha"]
                    self.pending_ci_sha = None
            elif ev_type == "DISPATCH_SENT":
                self.child_tasks += 1
                if ev.payload and ev.payload.get("routing_receipt_id"):
                    self.routing_receipts.append(ev.payload["routing_receipt_id"])
            elif ev_type == "RESULT_INGESTED":
                if ev.payload and ev.payload.get("result_id"):
                    self.verified_results.append(ev.payload["result_id"])
            elif ev_type == "POLICY_EVALUATED":
                if ev.payload and ev.payload.get("receipt_id"):
                    self.policy_receipts.append(ev.payload["receipt_id"])
            elif ev_type == "DECISION_ACCEPTED":
                decisions_count += 1
                if ev.payload:
                    action = ev.payload.get("action")
                    if action == "RETRY":
                        self.retries += 1
                        self.consecutive_failures += 1
                    elif action == "FIX":
                        self.fix_cycles += 1
                        self.consecutive_failures += 1
                    else:
                        self.consecutive_failures = 0

        self.iterations = max(proposals_count, decisions_count)

        # Test mock hook
        self._mock_astra_proposal: AstraNextActionProposal | None = None

    def _rehydrate_authority_context(self, state: SupervisorState) -> tuple[TaskIntent | None, Any | None, Any | None]:
        intent = getattr(self.loop, "_intents", {}).get(state.current_task_id) or getattr(self.loop, "_intents", {}).get(self.run_id)
        work_profile = getattr(self.loop, "_profiles", {}).get(self.run_id)
        effective_policy = getattr(self.loop, "_effective_policies", {}).get(self.run_id)

        events = self.store.load_events(self.run_id)
        from ai_engineering.task_intent import deserialize_intent
        from ai_engineering.supervisor.policy.work_profile import validate_work_profile

        for e in reversed(events):
            ev_type = getattr(e.event_type, "value", str(e.event_type))
            if intent is None and ev_type in ("RUN_INITIALIZED", "NEXT_TASK_GENERATED", "ATTEMPT_INCREMENTED"):
                if e.payload and "task_intent" in e.payload:
                    try:
                        ti_raw = e.payload["task_intent"]
                        intent = deserialize_intent(json.dumps(ti_raw) if isinstance(ti_raw, dict) else str(ti_raw))
                        if hasattr(self.loop, "_intents"):
                            self.loop._intents[intent.task_id] = intent
                            self.loop._intents[self.run_id] = intent
                    except Exception:
                        pass

            if (work_profile is None or effective_policy is None) and ev_type == "WORK_PROFILE_BOUND":
                if work_profile is None and e.payload and e.payload.get("work_profile"):
                    try:
                        wp_raw = dict(e.payload["work_profile"])
                        work_profile = validate_work_profile(wp_raw)
                    except Exception:
                        try:
                            wp_raw = dict(e.payload["work_profile"])
                            wp_raw.pop("profile_digest", None)
                            work_profile = validate_work_profile(wp_raw)
                        except Exception:
                            work_profile = None
                    if work_profile and hasattr(self.loop, "_profiles"):
                        self.loop._profiles[self.run_id] = work_profile
                if effective_policy is None and e.payload and e.payload.get("effective_policy"):
                    ep_payload = e.payload["effective_policy"]
                    try:
                        from ai_engineering.effective_policy import deserialize_effective_policy_report
                        ep_str = json.dumps(ep_payload) if isinstance(ep_payload, dict) else str(ep_payload)
                        effective_policy = deserialize_effective_policy_report(ep_str)
                    except Exception:
                        try:
                            from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus, TaskPolicyAttribution
                            d = dict(ep_payload) if isinstance(ep_payload, dict) else {}
                            tp = d.get("task_policy")
                            tpa = TaskPolicyAttribution(**tp) if isinstance(tp, dict) else tp
                            d["task_policy"] = tpa
                            if "status" in d and not isinstance(d["status"], EffectivePolicyStatus):
                                d["status"] = EffectivePolicyStatus(d["status"])
                            effective_policy = EffectivePolicyReport(**d)
                        except Exception:
                            effective_policy = None
                    if effective_policy and hasattr(self.loop, "_effective_policies"):
                        self.loop._effective_policies[self.run_id] = effective_policy

        return intent, work_profile, effective_policy

    async def _run_ci_wait_cycle(self, state: SupervisorState, target_sha: str) -> str:
        from ai_engineering.supervisor.events import create_event, SupervisorEventType

        def _record_ci_observation(obs_status: str, snap_dg: str = "") -> None:
            ci_receipt = getattr(self.ci_provider, "latest_receipt", None)
            ci_receipt_id = ci_receipt.receipt_id if ci_receipt else ""
            if ci_receipt_id and ci_receipt_id not in self.ci_receipts:
                self.ci_receipts.append(ci_receipt_id)

            cur_events = self.store.load_events(self.run_id)
            obs_ev = create_event(
                run_id=self.run_id,
                sequence=len(cur_events) + 1,
                previous_event_digest=cur_events[-1].event_digest if cur_events else None,
                event_type=SupervisorEventType.CI_STATUS_OBSERVED,
                state_revision=state.state_revision,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "exact_sha": target_sha,
                    "sha": target_sha,
                    "overall_status": obs_status,
                    "snapshot_digest": snap_dg,
                    "receipt_id": ci_receipt_id,
                },
                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            self.store.save_event(obs_ev)

        overall = "IN_PROGRESS"
        snap_dg = ""

        try:
            if hasattr(self.ci_provider, "check_ci"):
                timeout = getattr(self.ci_provider, "timeout_seconds", 600.0)
                interval = getattr(self.ci_provider, "poll_interval_seconds", 10.0)
                max_attempts = getattr(self.ci_provider, "max_poll_attempts", 60)
                start_time = time.monotonic()
                attempt = 0

                while True:
                    attempt += 1
                    elapsed = time.monotonic() - start_time
                    if elapsed >= timeout or attempt > max_attempts:
                        overall = "TIMED_OUT"
                        _record_ci_observation("TIMED_OUT")
                        break

                    snap = await self.ci_provider.check_ci(self.run_id, target_sha)
                    overall = snap.overall_status
                    snap_dg = getattr(snap, "snapshot_digest", "")
                    _record_ci_observation(overall, snap_dg)

                    if overall in ("SUCCESS", "FAILURE", "SHA_MISMATCH", "MISSING"):
                        break

                    await asyncio.sleep(interval)
            else:
                ci_res = await self.ci_provider.wait_for_ci(self.run_id, target_sha)
                if hasattr(ci_res, "overall_status"):
                    overall = ci_res.overall_status
                    snap_dg = getattr(ci_res, "snapshot_digest", "")
                elif isinstance(ci_res, bool):
                    overall = "SUCCESS" if ci_res else "FAILURE"
                    snap_dg = ""
                else:
                    overall = str(ci_res)
                    snap_dg = ""
                _record_ci_observation(overall, snap_dg)
        except Exception as e:
            code = getattr(e, "code", type(e).__name__)
            self.pending_ci_sha = None
            return f"CI_PROVIDER_FAILED_{code}"

        ci_receipt = getattr(self.ci_provider, "latest_receipt", None)
        ci_receipt_id = ci_receipt.receipt_id if ci_receipt else ""

        if overall == "SUCCESS":
            self.pending_ci_sha = None
            events = self.store.load_events(self.run_id)
            green_ev = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.CI_GREEN,
                state_revision=state.state_revision,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={"exact_sha": target_sha, "sha": target_sha},
                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            self.store.save_event(green_ev)
            return "SUCCESS"
        elif overall == "TIMED_OUT":
            self.pending_ci_sha = None
            events = self.store.load_events(self.run_id)
            to_ev = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.CI_WAIT_TIMEOUT,
                state_revision=state.state_revision,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={"exact_sha": target_sha, "sha": target_sha},
                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            self.store.save_event(to_ev)
            return "CI_WAIT_TIMEOUT"
        else:
            # FAILURE, MISSING, SHA_MISMATCH
            self.pending_ci_sha = None
            events = self.store.load_events(self.run_id)
            failed_ev = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.CI_FAILED,
                state_revision=state.state_revision,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "exact_sha": target_sha,
                    "sha": target_sha,
                    "result": overall,
                    "snapshot_digest": snap_dg,
                    "receipt_id": ci_receipt_id,
                },
                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            self.store.save_event(failed_ev)
            if overall == "SHA_MISMATCH":
                return "CI_SHA_MISMATCH"
            elif overall == "MISSING":
                return "CI_REQUIRED_CHECK_MISSING"
            else:
                return "CI_FAILED"

    async def run_until_terminal(self) -> AutonomousRunReceipt:
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        self.store.save_event(ev)

        while True:
            if self.iterations >= self.budget.max_supervisor_decisions:
                terminal_reason = "BUDGET_EXHAUSTED"
                break
            if self.child_tasks >= self.budget.max_child_tasks:
                terminal_reason = "BUDGET_EXHAUSTED"
                break
            if self.retries >= self.budget.max_retries:
                terminal_reason = "BUDGET_EXHAUSTED"
                break
            if self.fix_cycles >= self.budget.max_fix_cycles:
                terminal_reason = "BUDGET_EXHAUSTED"
                break
            if self.consecutive_failures >= self.budget.max_consecutive_failures:
                terminal_reason = "BUDGET_EXHAUSTED"
                break
            if self.provider_calls >= self.budget.max_provider_calls:
                terminal_reason = "BUDGET_EXHAUSTED"
                break

            state = self.store.load_state(self.run_id)

            if state.phase in (SupervisorPhase.DONE, SupervisorPhase.CANCELLED, SupervisorPhase.FAILED, SupervisorPhase.BLOCKED):
                terminal_reason = state.phase.value
                break

            # 1. TRUE CI RESTART RESUME:
            # If pending CI exists from before crash, resume CI polling directly before asking Astra!
            if self.pending_ci_sha:
                target_sha = self.pending_ci_sha
                ci_outcome = await self._run_ci_wait_cycle(state, target_sha)
                if ci_outcome != "SUCCESS":
                    terminal_reason = ci_outcome
                    break
                # CI succeeded! CI_GREEN was persisted, self.pending_ci_sha is cleared.
                # Continue loop so next iteration calls Astra for next proposal now that CI is green.
                continue

            self.iterations += 1

            try:
                proposal = await self._request_proposal(state)
            except Exception as e:
                code = getattr(e, "code", type(e).__name__)
                if code == "ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE":
                    terminal_reason = "ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE"
                else:
                    terminal_reason = f"ASTRA_PROVIDER_FAILED_{code}"
                break

            if proposal.action_type == NextActionType.STOP_SUCCESS:
                if not state.latest_verified_result_id:
                    terminal_reason = "STOP_SUCCESS_NOT_EVIDENCED"
                    break
                vr = None
                if hasattr(self.loop, "get_verified_result"):
                    vr = self.loop.get_verified_result(self.run_id, state.latest_verified_result_id)
                if not vr:
                    vr = self.loop._vr_cache.get(f"id:{state.latest_verified_result_id}") or self.loop._vr_cache.get(state.latest_verified_result_id)
                if not vr:
                    events = self.store.load_events(self.run_id)
                    from ai_engineering.supervisor.events import SupervisorEventType
                    from ai_engineering.supervisor.validator import deserialize_verified_result
                    for ev in reversed(events):
                        if getattr(ev.event_type, "value", str(ev.event_type)) == "RESULT_INGESTED":
                            if ev.payload and ev.payload.get("result_id") == state.latest_verified_result_id:
                                vr_data = ev.payload.get("verified_result")
                                if vr_data:
                                    vr = deserialize_verified_result(json.dumps(vr_data) if isinstance(vr_data, dict) else str(vr_data))
                                    self.loop._vr_cache[state.latest_verified_result_id] = vr
                                    break
                if not vr or getattr(vr.status, "value", str(vr.status)) != "PASS":
                    terminal_reason = "STOP_SUCCESS_NOT_EVIDENCED"
                    break
                if state.blockers:
                    terminal_reason = "STOP_SUCCESS_NOT_EVIDENCED"
                    break
                if any(getattr(gr.status, "value", str(gr.status)) != "PASS" for gr in vr.gate_results if gr.required):
                    terminal_reason = "STOP_SUCCESS_NOT_EVIDENCED"
                    break
                terminal_reason = "GOAL_COMPLETE"
                break
            elif proposal.action_type == NextActionType.STOP_BLOCKED:
                terminal_reason = "POLICY_BLOCKED"
                break

            if proposal.action_type == NextActionType.CREATE_PR:
                if self.pr_provider is None or not self.allow_pr_create:
                    events = self.store.load_events(self.run_id)
                    from ai_engineering.supervisor.events import create_event, SupervisorEventType
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_CREATE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "error_code": "PR_PROVIDER_UNAVAILABLE" if self.pr_provider is None else "PR_CREATION_NOT_ALLOWED",
                            "message": "PR provider is not available or PR creation is not allowed",
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_PROVIDER_UNAVAILABLE" if self.pr_provider is None else "PR_CREATION_NOT_ALLOWED"
                    break

                # 1. Gate on passing VerifiedResult
                vr = None
                if state.latest_verified_result_id:
                    if hasattr(self.loop, "get_verified_result"):
                        vr = self.loop.get_verified_result(self.run_id, state.latest_verified_result_id)
                    if not vr and hasattr(self.loop, "_vr_cache"):
                        vr = self.loop._vr_cache.get(f"id:{state.latest_verified_result_id}") or self.loop._vr_cache.get(state.latest_verified_result_id)
                    if not vr:
                        events = self.store.load_events(self.run_id)
                        from ai_engineering.supervisor.events import SupervisorEventType
                        from ai_engineering.supervisor.validator import deserialize_verified_result
                        for ev in reversed(events):
                            if getattr(ev.event_type, "value", str(ev.event_type)) == "RESULT_INGESTED":
                                if ev.payload and ev.payload.get("result_id") == state.latest_verified_result_id:
                                    vr_data = ev.payload.get("verified_result")
                                    if vr_data:
                                        vr = deserialize_verified_result(json.dumps(vr_data) if isinstance(vr_data, dict) else str(vr_data))
                                        break

                if not vr or getattr(vr.status, "value", str(vr.status)) != "PASS":
                    events = self.store.load_events(self.run_id)
                    from ai_engineering.supervisor.events import create_event, SupervisorEventType
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_CREATE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "error_code": "NO_PASSING_VERIFIED_RESULT",
                            "message": "PR creation requires a passing VerifiedResult",
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_CREATE_VERIFIED_RESULT_MISSING"
                    break

                candidate_head_sha = getattr(vr, "head_sha", "") or state.current_base_sha

                # 2. PolicyEngine Authorization for PR_MUTATION
                intent, work_profile, effective_policy = self._rehydrate_authority_context(state)
                if work_profile is None or effective_policy is None or intent is None:
                    terminal_reason = "ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE"
                    break

                from ai_engineering.supervisor.policy.contracts import (
                    PolicyRequest,
                    POLICY_REQUEST_SCHEMA_VERSION,
                    compute_deterministic_digest,
                    AutonomyLevel,
                    ExecutionTarget,
                    PolicyVerdict,
                )
                from ai_engineering.supervisor.policy.engine import evaluate_policy

                req_id = compute_deterministic_digest({
                    "run_id": self.run_id,
                    "task_id": state.current_task_id,
                    "action": "CREATE_PR",
                    "proposal_id": proposal.proposal_id,
                })

                target_sb = StopBoundary.READY_PR
                try:
                    if proposal.expected_stop_boundary:
                        target_sb = StopBoundary(proposal.expected_stop_boundary)
                except Exception:
                    pass

                pol_req = PolicyRequest(
                    schema_version=POLICY_REQUEST_SCHEMA_VERSION,
                    request_id=req_id,
                    run_id=self.run_id,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    decision_id=proposal.proposal_id,
                    decision_receipt_id=f"rec-{proposal.proposal_id}",
                    work_profile_id=work_profile.profile_id,
                    work_profile_digest=work_profile.profile_digest,
                    current_autonomy_level=state.autonomy_state.current_level if state.autonomy_state else AutonomyLevel.LEVEL_0_OBSERVE,
                    requested_action="CREATE_PR",
                    requested_effect_classes=(EffectClass.PR_MUTATION,),
                    requested_stop_boundary=target_sb,
                    execution_target=ExecutionTarget.DEV,
                    effective_policy_id=effective_policy.effective_policy_id,
                    effective_policy_digest=effective_policy.effective_policy_id,
                    budget_state_digest=state.budget_state.budget_digest if state.budget_state else "",
                )

                policy_receipt = evaluate_policy(
                    request=pol_req,
                    task_intent=intent,
                    effective_policy=effective_policy,
                    work_profile=work_profile,
                    autonomy_state=state.autonomy_state,
                    budget_state=state.budget_state,
                )

                events = self.store.load_events(self.run_id)
                from ai_engineering.supervisor.events import create_event, SupervisorEventType
                pol_ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.POLICY_EVALUATED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={
                        "receipt_id": policy_receipt.receipt_id,
                        "verdict": policy_receipt.verdict.value,
                        "reason_codes": list(policy_receipt.reason_codes),
                        "work_profile_id": work_profile.profile_id,
                        "current_level": state.autonomy_state.current_level.value if state.autonomy_state else "UNKNOWN",
                        "policy_receipt": __import__('dataclasses').asdict(policy_receipt),
                    },
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    decision_id=proposal.proposal_id,
                )
                self.store.save_event(pol_ev)
                self.policy_receipts.append(policy_receipt.receipt_id)

                if policy_receipt.verdict != PolicyVerdict.ALLOW:
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 2,
                        previous_event_digest=pol_ev.event_digest,
                        event_type=SupervisorEventType.PR_CREATE_FAILED,
                        state_revision=state.state_revision + 2,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "error_code": "POLICY_DENIED",
                            "reason_codes": list(policy_receipt.reason_codes),
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        decision_id=proposal.proposal_id,
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = f"POLICY_DENIED: {','.join(policy_receipt.reason_codes)}"
                    break

                # 3. Idempotent PR search / create
                repo = state.repository or intent.source_repository or "life2boat/hermes"
                head_branch = f"feat/{state.current_task_id}"
                base_branch = state.canonical_main_ref.split("/")[-1] if state.canonical_main_ref else "main"

                pr_action = "CREATED"
                existing_pr = await self.pr_provider.find_existing_pr(repo, head_branch, base_branch)
                events = self.store.load_events(self.run_id)

                if existing_pr:
                    pr_action = "RECOVERED"
                    pr_identity = existing_pr
                    if existing_pr.head_sha != candidate_head_sha:
                        head_change_ev = create_event(
                            run_id=self.run_id,
                            sequence=len(events) + 1,
                            previous_event_digest=events[-1].event_digest if events else None,
                            event_type=SupervisorEventType.PR_HEAD_CHANGED,
                            state_revision=state.state_revision + 1,
                            task_id=state.current_task_id,
                            attempt_id=state.current_attempt_id,
                            intent_digest=state.current_intent_digest,
                            payload={
                                "pr_number": existing_pr.pr_number,
                                "old_head_sha": existing_pr.head_sha,
                                "new_head_sha": candidate_head_sha,
                            },
                            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        )
                        self.store.save_event(head_change_ev)
                        events.append(head_change_ev)

                    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    rcpt_payload = {
                        "schema_version": PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
                        "run_id": self.run_id,
                        "task_id": state.current_task_id,
                        "policy_receipt_id": policy_receipt.receipt_id,
                        "repository": repo,
                        "pr_number": existing_pr.pr_number,
                        "head_branch": existing_pr.head_branch,
                        "head_sha": existing_pr.head_sha,
                        "base_branch": existing_pr.base_branch,
                        "base_sha": existing_pr.base_sha,
                        "action": pr_action,
                        "created_at_utc": now_str,
                    }
                    pr_dg = compute_pr_receipt_digest(rcpt_payload)
                    pr_receipt = PullRequestReceipt(
                        schema_version=PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
                        receipt_id=f"pr-rcpt-{existing_pr.pr_number}-{self.run_id}",
                        run_id=self.run_id,
                        task_id=state.current_task_id,
                        policy_receipt_id=policy_receipt.receipt_id,
                        repository=repo,
                        pr_number=existing_pr.pr_number,
                        head_branch=existing_pr.head_branch,
                        head_sha=existing_pr.head_sha,
                        base_branch=existing_pr.base_branch,
                        base_sha=existing_pr.base_sha,
                        action=pr_action,
                        created_at_utc=now_str,
                        receipt_digest=pr_dg,
                    )

                    rec_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_RECOVERED,
                        state_revision=state.state_revision + 1,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "pr_number": existing_pr.pr_number,
                            "head_branch": existing_pr.head_branch,
                            "head_sha": existing_pr.head_sha,
                            "base_branch": existing_pr.base_branch,
                            "base_sha": existing_pr.base_sha,
                            "repository": repo,
                            "receipt_id": pr_receipt.receipt_id,
                        },
                        created_at_utc=now_str,
                    )
                    self.store.save_event(rec_ev)
                else:
                    req_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_CREATE_REQUESTED,
                        state_revision=state.state_revision + 1,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "repository": repo,
                            "head_branch": head_branch,
                            "base_branch": base_branch,
                            "title": f"feat: {state.root_goal or state.current_task_id}",
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(req_ev)
                    events.append(req_ev)

                    is_draft = (target_sb == StopBoundary.DRAFT_PR)
                    pr_identity = await self.pr_provider.create_pr(
                        repository=repo,
                        head_branch=head_branch,
                        base_branch=base_branch,
                        title=f"feat: {state.root_goal or state.current_task_id}",
                        body=f"Autonomous PR lifecycle for task {state.current_task_id}\nObjective: {proposal.objective}",
                        draft=is_draft,
                        head_sha=candidate_head_sha,
                    )

                    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    rcpt_payload = {
                        "schema_version": PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
                        "run_id": self.run_id,
                        "task_id": state.current_task_id,
                        "policy_receipt_id": policy_receipt.receipt_id,
                        "repository": repo,
                        "pr_number": pr_identity.pr_number,
                        "head_branch": pr_identity.head_branch,
                        "head_sha": pr_identity.head_sha,
                        "base_branch": pr_identity.base_branch,
                        "base_sha": pr_identity.base_sha,
                        "action": pr_action,
                        "created_at_utc": now_str,
                    }
                    pr_dg = compute_pr_receipt_digest(rcpt_payload)
                    pr_receipt = PullRequestReceipt(
                        schema_version=PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
                        receipt_id=f"pr-rcpt-{pr_identity.pr_number}-{self.run_id}",
                        run_id=self.run_id,
                        task_id=state.current_task_id,
                        policy_receipt_id=policy_receipt.receipt_id,
                        repository=repo,
                        pr_number=pr_identity.pr_number,
                        head_branch=pr_identity.head_branch,
                        head_sha=pr_identity.head_sha,
                        base_branch=pr_identity.base_branch,
                        base_sha=pr_identity.base_sha,
                        action=pr_action,
                        created_at_utc=now_str,
                        receipt_digest=pr_dg,
                    )

                    created_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_CREATED,
                        state_revision=state.state_revision + 1,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "pr_number": pr_identity.pr_number,
                            "head_branch": pr_identity.head_branch,
                            "head_sha": pr_identity.head_sha,
                            "base_branch": pr_identity.base_branch,
                            "base_sha": pr_identity.base_sha,
                            "repository": repo,
                            "is_draft": pr_identity.is_draft,
                            "receipt_id": pr_receipt.receipt_id,
                        },
                        created_at_utc=now_str,
                    )
                    self.store.save_event(created_ev)

                self.pr_receipts.append(pr_receipt.receipt_id)
                self.active_pr = pr_identity
                self.active_pr_number = pr_identity.pr_number
                self.active_pr_head_sha = pr_identity.head_sha
                continue

            if proposal.action_type == NextActionType.WAIT_FOR_CI:
                target_sha = self.active_pr_head_sha or self.pending_ci_sha or state.current_base_sha
                self.pending_ci_sha = target_sha

                repo = state.repository or (intent.source_repository if intent else "life2boat/hermes")
                if self.pr_provider and self.active_pr_number:
                    try:
                        cur_pr = await self.pr_provider.get_pr(repo, self.active_pr_number)
                        if cur_pr.head_sha != target_sha:
                            events = self.store.load_events(self.run_id)
                            from ai_engineering.supervisor.events import create_event, SupervisorEventType
                            chg_ev = create_event(
                                run_id=self.run_id,
                                sequence=len(events) + 1,
                                previous_event_digest=events[-1].event_digest if events else None,
                                event_type=SupervisorEventType.PR_HEAD_CHANGED,
                                state_revision=state.state_revision,
                                task_id=state.current_task_id,
                                attempt_id=state.current_attempt_id,
                                intent_digest=state.current_intent_digest,
                                payload={
                                    "pr_number": self.active_pr_number,
                                    "old_head_sha": target_sha,
                                    "new_head_sha": cur_pr.head_sha,
                                },
                                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                            )
                            self.store.save_event(chg_ev)
                            self.active_pr_head_sha = cur_pr.head_sha
                            self.pending_ci_sha = None
                            terminal_reason = "PR_HEAD_CHANGED"
                            break
                    except Exception:
                        pass

                events = self.store.load_events(self.run_id)
                from ai_engineering.supervisor.events import create_event, SupervisorEventType
                start_ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.CI_WAIT_STARTED,
                    state_revision=state.state_revision,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={"sha": target_sha, "exact_sha": target_sha},
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                )
                self.store.save_event(start_ev)

                ci_outcome = await self._run_ci_wait_cycle(state, target_sha)
                if ci_outcome != "SUCCESS":
                    terminal_reason = ci_outcome
                    break
                continue

            if proposal.action_type == NextActionType.MERGE_IF_GREEN:
                if self.pr_provider is None or not self.allow_pr_merge:
                    events = self.store.load_events(self.run_id)
                    from ai_engineering.supervisor.events import create_event, SupervisorEventType
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "error_code": "MERGE_PROVIDER_UNAVAILABLE" if self.pr_provider is None else "PR_MERGE_NOT_ALLOWED",
                            "message": "PR provider unavailable or merge not allowed",
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "MERGE_PROVIDER_UNAVAILABLE" if self.pr_provider is None else "PR_MERGE_NOT_ALLOWED"
                    break

                if not self.active_pr_number:
                    events = self.store.load_events(self.run_id)
                    from ai_engineering.supervisor.events import create_event, SupervisorEventType
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={"error_code": "NO_ACTIVE_PR", "message": "No active PR to merge"},
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_NOT_FOUND"
                    break

                # PolicyEngine Authorization for MERGE
                intent, work_profile, effective_policy = self._rehydrate_authority_context(state)
                if work_profile is None or effective_policy is None or intent is None:
                    terminal_reason = "ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE"
                    break

                from ai_engineering.supervisor.policy.contracts import (
                    PolicyRequest,
                    POLICY_REQUEST_SCHEMA_VERSION,
                    compute_deterministic_digest,
                    AutonomyLevel,
                    ExecutionTarget,
                    PolicyVerdict,
                )
                from ai_engineering.supervisor.policy.engine import evaluate_policy

                req_id = compute_deterministic_digest({
                    "run_id": self.run_id,
                    "task_id": state.current_task_id,
                    "action": "MERGE_IF_GREEN",
                    "proposal_id": proposal.proposal_id,
                })

                pol_req = PolicyRequest(
                    schema_version=POLICY_REQUEST_SCHEMA_VERSION,
                    request_id=req_id,
                    run_id=self.run_id,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    decision_id=proposal.proposal_id,
                    decision_receipt_id=f"rec-{proposal.proposal_id}",
                    work_profile_id=work_profile.profile_id,
                    work_profile_digest=work_profile.profile_digest,
                    current_autonomy_level=state.autonomy_state.current_level if state.autonomy_state else AutonomyLevel.LEVEL_0_OBSERVE,
                    requested_action="MERGE_IF_GREEN",
                    requested_effect_classes=(EffectClass.PR_MERGE,),
                    requested_stop_boundary=StopBoundary.MERGE,
                    execution_target=ExecutionTarget.DEV,
                    effective_policy_id=effective_policy.effective_policy_id,
                    effective_policy_digest=effective_policy.effective_policy_id,
                    budget_state_digest=state.budget_state.budget_digest if state.budget_state else "",
                )

                policy_receipt = evaluate_policy(
                    request=pol_req,
                    task_intent=intent,
                    effective_policy=effective_policy,
                    work_profile=work_profile,
                    autonomy_state=state.autonomy_state,
                    budget_state=state.budget_state,
                )

                events = self.store.load_events(self.run_id)
                from ai_engineering.supervisor.events import create_event, SupervisorEventType
                pol_ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.POLICY_EVALUATED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={
                        "receipt_id": policy_receipt.receipt_id,
                        "verdict": policy_receipt.verdict.value,
                        "reason_codes": list(policy_receipt.reason_codes),
                        "work_profile_id": work_profile.profile_id,
                        "current_level": state.autonomy_state.current_level.value if state.autonomy_state else "UNKNOWN",
                        "policy_receipt": __import__('dataclasses').asdict(policy_receipt),
                    },
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    decision_id=proposal.proposal_id,
                )
                self.store.save_event(pol_ev)
                self.policy_receipts.append(policy_receipt.receipt_id)

                if policy_receipt.verdict != PolicyVerdict.ALLOW:
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 2,
                        previous_event_digest=pol_ev.event_digest,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision + 2,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "error_code": "POLICY_DENIED",
                            "reason_codes": list(policy_receipt.reason_codes),
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        decision_id=proposal.proposal_id,
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = f"POLICY_DENIED: {','.join(policy_receipt.reason_codes)}"
                    break

                # Fresh Merge Gate Revalidation
                repo = state.repository or intent.source_repository or "life2boat/hermes"
                cur_pr = await self.pr_provider.get_pr(repo, self.active_pr_number)

                if cur_pr.merged:
                    rec_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 2,
                        previous_event_digest=pol_ev.event_digest,
                        event_type=SupervisorEventType.PR_MERGE_RECOVERED,
                        state_revision=state.state_revision + 2,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "pr_number": cur_pr.pr_number,
                            "merged_commit_sha": cur_pr.merge_commit_sha,
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(rec_ev)
                    self.merged_commit_sha = cur_pr.merge_commit_sha
                    terminal_reason = "GOAL_COMPLETE"
                    break

                if cur_pr.state != "open":
                    events = self.store.load_events(self.run_id)
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={"error_code": "PR_NOT_OPEN", "state": cur_pr.state},
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_NOT_OPEN"
                    break

                if cur_pr.is_draft:
                    events = self.store.load_events(self.run_id)
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={"error_code": "PR_IS_DRAFT"},
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_IS_DRAFT"
                    break

                if cur_pr.mergeable is False or cur_pr.mergeable_state == "dirty":
                    events = self.store.load_events(self.run_id)
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={"error_code": "PR_CONFLICT", "mergeable_state": cur_pr.mergeable_state},
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_CONFLICT"
                    break

                if self.active_pr_head_sha and cur_pr.head_sha != self.active_pr_head_sha:
                    events = self.store.load_events(self.run_id)
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_HEAD_CHANGED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "pr_number": cur_pr.pr_number,
                            "old_head_sha": self.active_pr_head_sha,
                            "new_head_sha": cur_pr.head_sha,
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "PR_HEAD_MISMATCH"
                    break

                # Verify fresh green CI
                ci_green = False
                events = self.store.load_events(self.run_id)
                for ev in reversed(events):
                    if getattr(ev.event_type, "value", str(ev.event_type)) == "CI_GREEN":
                        if ev.payload and (ev.payload.get("exact_sha") == cur_pr.head_sha or ev.payload.get("sha") == cur_pr.head_sha):
                            ci_green = True
                            break

                if not ci_green:
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={"error_code": "CI_NOT_GREEN", "sha": cur_pr.head_sha},
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = "CI_NOT_GREEN"
                    break

                # Execute Atomic Squash Merge
                events = self.store.load_events(self.run_id)
                req_ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.PR_MERGE_REQUESTED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={
                        "repository": repo,
                        "pr_number": self.active_pr_number,
                        "exact_head_sha": cur_pr.head_sha,
                        "merge_method": "squash",
                    },
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                self.store.save_event(req_ev)

                try:
                    merge_receipt = await self.pr_provider.merge_pr(
                        repository=repo,
                        pr_number=self.active_pr_number,
                        exact_head_sha=cur_pr.head_sha,
                        merge_method="squash",
                    )
                except Exception as e:
                    code = getattr(e, "code", type(e).__name__)
                    events = self.store.load_events(self.run_id)
                    fail_ev = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.PR_MERGE_FAILED,
                        state_revision=state.state_revision + 1,
                        task_id=state.current_task_id,
                        attempt_id=state.current_attempt_id,
                        intent_digest=state.current_intent_digest,
                        payload={
                            "error_code": code,
                            "error_message": str(e),
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    self.store.save_event(fail_ev)
                    terminal_reason = f"PR_MERGE_FAILED_{code}"
                    break

                self.merge_receipts.append(merge_receipt.receipt_id)
                self.merged_commit_sha = merge_receipt.merged_commit_sha

                events = self.store.load_events(self.run_id)
                merged_ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.PR_MERGED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={
                        "pr_number": self.active_pr_number,
                        "merged_commit_sha": merge_receipt.merged_commit_sha,
                        "receipt_id": merge_receipt.receipt_id,
                    },
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                self.store.save_event(merged_ev)

                recon_ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 2,
                    previous_event_digest=merged_ev.event_digest,
                    event_type=SupervisorEventType.SOURCE_RECONCILED,
                    state_revision=state.state_revision + 2,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={
                        "previous_base_sha": state.current_base_sha,
                        "new_base_sha": merge_receipt.merged_commit_sha,
                    },
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                self.store.save_event(recon_ev)

                terminal_reason = "GOAL_COMPLETE"
                break

            decision = self._convert_proposal_to_decision(proposal, state)

            try:
                decision_receipt, next_state = self.loop.accept_decision(
                    run_id=self.run_id,
                    decision=decision,
                    context_pack_digest=decision.context_pack_digest,
                    verified_result_status="FAIL" if proposal.action_type == NextActionType.FIX else ("BLOCKED" if proposal.action_type == NextActionType.RETRY else "PASS"),
                    validated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                )
                events = self.store.load_events(self.run_id)
                policy_event = next(e for e in reversed(events) if getattr(e.event_type, "value", str(e.event_type)) == "POLICY_EVALUATED")
                pr_data = policy_event.payload["policy_receipt"]
                from ai_engineering.supervisor.policy.contracts import PolicyReceipt, PolicyVerdict
                pr_data_copy = dict(pr_data)
                pr_data_copy["verdict"] = PolicyVerdict(pr_data["verdict"])
                # Extract reason codes to tuple
                pr_data_copy["reason_codes"] = tuple(pr_data["reason_codes"])
                receipt = PolicyReceipt(**pr_data_copy)

                self.policy_receipts.append(receipt.receipt_id)

                action_val = getattr(decision.action, "value", str(decision.action))
                if action_val == "RETRY":
                    self.retries += 1
                    self.consecutive_failures += 1
                elif action_val == "FIX":
                    self.fix_cycles += 1
                    self.consecutive_failures += 1
                else:
                    self.consecutive_failures = 0
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
                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                expires_at_utc=""
            )

            try:
                worker_bundle = await self.router.dispatch(envelope)
                import os
                routing_receipt_id = None
                store_root = getattr(self.router.store, "root_dir", None) or getattr(self.router.store, "_root", None)
                if store_root:
                    file_path = os.path.join(store_root, f"{envelope.message_id}.json")
                    if os.path.exists(file_path):
                        with open(file_path, "r", encoding="utf-8") as rf:
                            rd = __import__('json').load(rf)
                        if rd.get("routing_receipt") and rd["routing_receipt"].get("routing_receipt_id"):
                            routing_receipt_id = rd["routing_receipt"]["routing_receipt_id"]

                if not routing_receipt_id:
                    from ai_engineering.supervisor.events import create_event, SupervisorEventType
                    events = self.store.load_events(self.run_id)
                    blocker_event = create_event(
                        run_id=self.run_id,
                        sequence=len(events) + 1,
                        previous_event_digest=events[-1].event_digest if events else None,
                        event_type=SupervisorEventType.BLOCKER_RECORDED,
                        state_revision=next_state.state_revision + 1,
                        task_id=next_state.current_task_id,
                        attempt_id=next_state.current_attempt_id,
                        intent_digest=next_state.current_intent_digest,
                        payload={
                            "blocker": "ROUTING_RECEIPT_MISSING",
                            "reason_codes": ["ROUTING_RECEIPT_MISSING"],
                        },
                        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    )
                    self.store.save_event(blocker_event)
                    terminal_reason = "BLOCKED"
                    break

                self.routing_receipts.append(routing_receipt_id)
                self.child_tasks += 1

                from ai_engineering.supervisor.events import create_event, SupervisorEventType
                events = self.store.load_events(self.run_id)
                dispatch_event = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.DISPATCH_SENT,
                    state_revision=next_state.state_revision,
                    task_id=next_state.current_task_id,
                    attempt_id=next_state.current_attempt_id,
                    intent_digest=next_state.current_intent_digest,
                    payload={
                        "message_id": envelope.message_id,
                        "worker_id": worker_bundle.worker_id,
                        "routing_receipt_id": routing_receipt_id,
                    },
                    created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
                )
                self.store.save_event(dispatch_event)

                intent = self.loop._intents.get(next_state.current_task_id) or self.loop._intents.get(self.run_id)
                if intent is None:
                    events = self.store.load_events(self.run_id)
                    from ai_engineering.task_intent import deserialize_intent
                    for e in reversed(events):
                        ev_type = getattr(e.event_type, "value", str(e.event_type))
                        if ev_type in ("RUN_INITIALIZED", "NEXT_TASK_GENERATED", "ATTEMPT_INCREMENTED"):
                            if "task_intent" in e.payload:
                                intent = deserialize_intent(json.dumps(e.payload["task_intent"]))
                                self.loop._intents[intent.task_id] = intent
                                self.loop._intents[self.run_id] = intent
                                break

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

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        final_state = self.store.load_state(self.run_id)

        import json
        budget_dict = {
            "max_supervisor_decisions": self.budget.max_supervisor_decisions,
            "max_child_tasks": self.budget.max_child_tasks,
            "max_retries": self.budget.max_retries,
            "max_fix_cycles": self.budget.max_fix_cycles,
            "max_consecutive_failures": self.budget.max_consecutive_failures,
            "max_provider_calls": self.budget.max_provider_calls
        }

        final_sha = self.merged_commit_sha or final_state.current_base_sha
        receipt_obj = AutonomousRunReceipt(
            schema_version="hermes.autonomous-run-receipt.v1",
            run_id=self.run_id,
            root_task_intent_digest=final_state.current_intent_digest,
            initial_source_sha=initial_sha,
            final_source_sha=final_sha,
            iterations=self.iterations,
            child_tasks=self.child_tasks,
            budgets=budget_dict,
            policy_receipts=self.policy_receipts,
            routing_receipts=self.routing_receipts,
            verified_results=self.verified_results,
            terminal_reason=terminal_reason,
            started_at=started_at,
            completed_at=completed_at,
            receipt_digest="",
            astra_receipts=self.astra_receipts,
            ci_receipts=self.ci_receipts,
            pr_receipts=self.pr_receipts,
            merge_receipts=self.merge_receipts,
            pr_number=self.active_pr_number,
            merged_commit_sha=self.merged_commit_sha,
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
        self.provider_calls += 1

        from ai_engineering.supervisor.events import create_event, SupervisorEventType
        events = self.store.load_events(self.run_id)
        req_ev = create_event(
            run_id=self.run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.ASTRA_REQUESTED,
            state_revision=state.state_revision,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={"run_id": self.run_id, "task_id": state.current_task_id},
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        self.store.save_event(req_ev)

        intent, work_profile, effective_policy = self._rehydrate_authority_context(state)

        if (
            getattr(self, "provider_mode", "real") == "real"
            or type(self.astra_provider).__name__ == "ConfiguredAstraProposalProvider"
        ):
            if work_profile is None or effective_policy is None or intent is None:
                from ai_engineering.supervisor.astra_provider import AstraProviderUnavailableError
                raise AstraProviderUnavailableError(
                    "ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE: WorkProfile, EffectivePolicy, and TaskIntent must be present in real provider mode",
                    code="ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE",
                )

        stats = {
            "iterations": self.iterations,
            "child_tasks": self.child_tasks,
            "retries": self.retries,
            "fix_cycles": self.fix_cycles,
            "consecutive_failures": self.consecutive_failures,
            "provider_calls": self.provider_calls,
        }

        vr = None
        if state.latest_verified_result_id:
            if hasattr(self.loop, "get_verified_result"):
                vr = self.loop.get_verified_result(self.run_id, state.latest_verified_result_id)
            if not vr and hasattr(self.loop, "_vr_cache"):
                vr = self.loop._vr_cache.get(f"id:{state.latest_verified_result_id}") or self.loop._vr_cache.get(state.latest_verified_result_id)
            if not vr:
                events = self.store.load_events(self.run_id)
                from ai_engineering.supervisor.validator import deserialize_verified_result
                for ev in reversed(events):
                    if getattr(ev.event_type, "value", str(ev.event_type)) == "RESULT_INGESTED":
                        if ev.payload and ev.payload.get("result_id") == state.latest_verified_result_id:
                            vr_data = ev.payload.get("verified_result")
                            if vr_data:
                                vr = deserialize_verified_result(json.dumps(vr_data) if isinstance(vr_data, dict) else str(vr_data))
                                if hasattr(self.loop, "_vr_cache"):
                                    self.loop._vr_cache[state.latest_verified_result_id] = vr
                                break

        pack_dg = ""
        if intent is not None and hasattr(self.loop, "build_context_pack"):
            try:
                pack, _ = self.loop.build_context_pack(self.run_id, intent)
                from ai_engineering.supervisor.context_pack import context_pack_digest
                pack_dg = context_pack_digest(pack)
            except Exception:
                pass

        try:
            if self._mock_astra_proposal:
                proposal = self._mock_astra_proposal
            else:
                is_real_mode = (getattr(self, "provider_mode", "real") == "real")
                sig = inspect.signature(self.astra_provider.request_proposal)
                has_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                kw = {}
                if has_kw or "intent" in sig.parameters:
                    kw["intent"] = intent
                if has_kw or "budget" in sig.parameters:
                    kw["budget"] = self.budget
                if has_kw or "stats" in sig.parameters:
                    kw["stats"] = stats
                if has_kw or "work_profile" in sig.parameters:
                    kw["work_profile"] = work_profile
                if has_kw or "effective_policy" in sig.parameters:
                    kw["effective_policy"] = effective_policy
                if has_kw or "verified_result" in sig.parameters:
                    kw["verified_result"] = vr
                if has_kw or "context_pack_digest" in sig.parameters:
                    kw["context_pack_digest"] = pack_dg
                if has_kw or "real_mode" in sig.parameters:
                    kw["real_mode"] = is_real_mode

                if is_real_mode:
                    # In provider_mode=real: NEVER retry without trusted authority context.
                    # Any TypeError/ValueError from real provider path must fail closed.
                    proposal = await self.astra_provider.request_proposal(self.run_id, state, **kw)
                else:
                    try:
                        proposal = await self.astra_provider.request_proposal(self.run_id, state, **kw)
                    except (TypeError, ValueError):
                        proposal = await self.astra_provider.request_proposal(self.run_id, state)
        except Exception as e:
            receipt = getattr(self.astra_provider, "latest_receipt", None)
            receipt_id = receipt.receipt_id if receipt else ""
            if receipt_id and receipt_id not in self.astra_receipts:
                self.astra_receipts.append(receipt_id)

            events = self.store.load_events(self.run_id)
            fail_ev = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.ASTRA_PROVIDER_FAILED,
                state_revision=state.state_revision,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "error_type": type(e).__name__,
                    "error_code": getattr(e, "code", type(e).__name__),
                    "error_message": str(e),
                },
                created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            self.store.save_event(fail_ev)
            raise

        receipt = getattr(self.astra_provider, "latest_receipt", None)
        receipt_id = receipt.receipt_id if receipt else ""
        if receipt_id:
            self.astra_receipts.append(receipt_id)

        events = self.store.load_events(self.run_id)
        ev = create_event(
            run_id=self.run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.ASTRA_PROPOSAL_CREATED,
            state_revision=state.state_revision,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "proposal_id": proposal.proposal_id,
                "action_type": getattr(proposal.action_type, "value", str(proposal.action_type)),
                "receipt_id": receipt_id,
            },
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        self.store.save_event(ev)
        return proposal

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

        intent = self.loop._intents.get(state.current_task_id) or self.loop._intents.get(self.run_id)
        if intent is None:
            events = self.store.load_events(self.run_id)
            from ai_engineering.task_intent import deserialize_intent
            for e in reversed(events):
                ev_type = getattr(e.event_type, "value", str(e.event_type))
                if ev_type in ("RUN_INITIALIZED", "NEXT_TASK_GENERATED", "ATTEMPT_INCREMENTED"):
                    if "task_intent" in e.payload:
                        intent = deserialize_intent(json.dumps(e.payload["task_intent"]))
                        self.loop._intents[intent.task_id] = intent
                        self.loop._intents[self.run_id] = intent
                        break

        pack, _ = self.loop.build_context_pack(self.run_id, intent)
        from ai_engineering.supervisor.context_pack import context_pack_digest
        pack_dg = context_pack_digest(pack)

        vr_digest = state.latest_verified_result_digest or ""
        if not vr_digest and state.latest_verified_result_id:
            if hasattr(self.loop, "get_verified_result"):
                self.loop.get_verified_result(self.run_id, state.latest_verified_result_id)
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
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
