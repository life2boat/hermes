from __future__ import annotations
import os
import json
import uuid
import hashlib
import asyncio
import datetime
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence, Iterable
from pathlib import Path

from ai_engineering.contracts import EffectClass, StopBoundary, Status
from ai_engineering.task_intent import TaskIntent, intent_digest
from ai_engineering.supervisor.policy.contracts import PolicyReceipt, PolicyVerdict
from ai_engineering.supervisor.worker_result import WorkerResultBundle, deserialize_worker_result, canonical_serialize_worker_result
from ai_engineering.supervisor.events import SupervisorEventType
from ai_engineering.control_plane.orchestrator import _STOP_BOUNDARY_RANK
from .envelope import AgentEnvelope, MessageType
from .adapters import (
    AgentAdapter,
    AgentTransportUnavailableError,
    PolicyDeniedError,
    ComputerUseTransportUnavailableError,
)
from .registry import AgentRegistry

# ── Router Exceptions ───────────────────────────────────────────────────────

class RouterError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)

class AuthorityInvalidError(RouterError): pass
class EffectClassEscalationError(RouterError): pass
class StopBoundaryEscalationError(RouterError): pass
class MessageTamperedError(RouterError): pass
class CrossRunViolationError(RouterError): pass
class UnsupportedMessageTypeError(RouterError): pass
class StaleResultError(RouterError): pass
class WorkerResultIdentityMismatchError(RouterError): pass
class IllegalStateTransitionError(RouterError): pass
class CapabilityNotAuthorizedError(RouterError): pass
class SourceProvenanceUnresolvedError(RouterError): pass
class SourceProvenanceMismatchError(RouterError): pass
class TimeoutCancellationUnconfirmedError(RouterError): pass
class ReplayMessageInvalidError(RouterError): pass
class StaleOrForeignAttemptError(RouterError): pass

NON_RETRYABLE_EXCEPTIONS = frozenset({
    AuthorityInvalidError,
    PolicyDeniedError,
    MessageTamperedError,
    UnsupportedMessageTypeError,
    EffectClassEscalationError,
    StopBoundaryEscalationError,
    CrossRunViolationError,
    WorkerResultIdentityMismatchError,
    CapabilityNotAuthorizedError,
    SourceProvenanceUnresolvedError,
    SourceProvenanceMismatchError,
    TimeoutCancellationUnconfirmedError,
    ReplayMessageInvalidError,
    StaleOrForeignAttemptError,
    IllegalStateTransitionError,
})

# ── Routing Receipt ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RoutingReceipt:
    routing_receipt_id: str
    message_id: str
    correlation_id: str
    run_id: str
    task_id: str
    attempt_id: str
    requested_capability: str
    selected_agent: str
    task_intent_id: str
    policy_receipt_id: str
    effective_effect_class: str
    effective_stop_boundary: str
    decision: str  # "ROUTE", "BLOCK", "DEFER"
    reason: str
    attempt_number: int
    created_at_utc: str
    schema_version: str = "hermes.agent-routing-receipt.v1"

def compute_policy_receipt_digest(receipt: PolicyReceipt) -> str:
    canonical = {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "request_id": receipt.request_id,
        "task_intent_digest": receipt.task_intent_digest,
        "decision_id": receipt.decision_id,
        "decision_receipt_id": receipt.decision_receipt_id,
        "effective_policy_id": receipt.effective_policy_id,
        "work_profile_id": receipt.work_profile_id,
        "work_profile_digest": receipt.work_profile_digest,
        "autonomy_state_digest": receipt.autonomy_state_digest,
        "budget_state_digest": receipt.budget_state_digest,
        "verdict": receipt.verdict.value if hasattr(receipt.verdict, "value") else str(receipt.verdict),
        "reason_codes": sorted(list(receipt.reason_codes)),
        "created_at_utc": receipt.created_at_utc,
    }
    dumped = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(dumped).hexdigest()

# ── Authority Resolver ──────────────────────────────────────────────────────

class AuthorityResolver:
    def __init__(
        self,
        supervisor_store: Any | None = None,
        supervisor_loop: Any | None = None,
        intent_store: dict[tuple[str, str], TaskIntent] | None = None,
        receipt_store: dict[tuple[str, str], PolicyReceipt] | None = None,
        provenance_store: dict[str, dict[str, str]] | None = None,
        permitted_capabilities_store: dict[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> None:
        self.supervisor_store = supervisor_store
        self.supervisor_loop = supervisor_loop
        self.intent_store: dict[tuple[str, str], TaskIntent] = intent_store if intent_store is not None else {}
        self.receipt_store: dict[tuple[str, str], PolicyReceipt] = receipt_store if receipt_store is not None else {}
        self.provenance_store: dict[str, dict[str, str]] = provenance_store if provenance_store is not None else {}
        self.permitted_capabilities_store: dict[tuple[str, str], tuple[str, ...]] = (
            permitted_capabilities_store if permitted_capabilities_store is not None else {}
        )

    def register_authority(
        self,
        run_id: str,
        task_id: str,
        intent: TaskIntent,
        receipt: PolicyReceipt,
        provenance: dict[str, str] | None = None,
        permitted_capabilities: Sequence[str] | None = None,
    ) -> None:
        self.intent_store[(run_id, task_id)] = intent
        self.receipt_store[(run_id, task_id)] = receipt
        if provenance is not None:
            self.provenance_store[run_id] = dict(provenance)
        else:
            self.provenance_store[run_id] = {
                "repository": intent.source_repository,
                "canonical_remote": "github",
                "base_sha": intent.source_base_sha,
            }
        if permitted_capabilities is not None:
            self.permitted_capabilities_store[(run_id, task_id)] = tuple(permitted_capabilities)

    def resolve_task_intent(
        self,
        intent_id: str,
        digest: str,
        run_id: str,
        task_id: str,
    ) -> TaskIntent:
        stored = None
        if self.supervisor_loop is not None and hasattr(self.supervisor_loop, "_intents"):
            stored = self.supervisor_loop._intents.get(task_id) or self.supervisor_loop._intents.get(run_id)
        if stored is None:
            stored = self.intent_store.get((run_id, task_id))
        if stored is None and self.supervisor_store is not None:
            try:
                events = self.supervisor_store.load_events(run_id)
                for e in reversed(events):
                    ev_type = getattr(e.event_type, "value", str(e.event_type))
                    if ev_type in ("RUN_INITIALIZED", "NEXT_TASK_GENERATED", "ATTEMPT_INCREMENTED"):
                        if "task_intent" in e.payload and e.task_id == task_id:
                            from ai_engineering.task_intent import deserialize_intent
                            import json
                            stored = deserialize_intent(json.dumps(e.payload["task_intent"]))
                            break
            except Exception:
                pass

        if stored is None:
            raise AuthorityInvalidError(f"AUTHORITY_INVALID: TaskIntent not found for run={run_id} task={task_id}")
        if stored.task_id != intent_id:
            raise AuthorityInvalidError(f"AUTHORITY_INVALID: TaskIntent id mismatch {stored.task_id} != {intent_id}")
        expected_digest = intent_digest(stored)
        if expected_digest != digest:
            raise AuthorityInvalidError("AUTHORITY_INVALID: TaskIntent digest mismatch")
        return stored

    def resolve_policy_receipt(
        self,
        receipt_id: str,
        digest: str,
        run_id: str,
        task_id: str,
    ) -> PolicyReceipt:
        stored = None
        if self.supervisor_store is not None:
            try:
                events = self.supervisor_store.load_events(run_id)
                for e in reversed(events):
                    if (
                        e.event_type == SupervisorEventType.POLICY_EVALUATED
                        or getattr(e.event_type, "value", str(e.event_type)) == "POLICY_EVALUATED"
                    ):
                        if e.payload.get("receipt_id") == receipt_id or (
                            isinstance(e.payload.get("policy_receipt"), dict)
                            and e.payload["policy_receipt"].get("receipt_id") == receipt_id
                        ):
                            pr_data = e.payload.get("policy_receipt", {})
                            if pr_data:
                                verdict_val = pr_data.get("verdict", "ALLOW")
                                stored = PolicyReceipt(
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
                                    verdict=PolicyVerdict(verdict_val) if hasattr(PolicyVerdict, verdict_val) else PolicyVerdict[verdict_val],
                                    reason_codes=tuple(pr_data.get("reason_codes", ())),
                                    created_at_utc=pr_data.get("created_at_utc", ""),
                                )
                                break
            except Exception:
                pass

        if stored is None:
            stored = self.receipt_store.get((run_id, task_id))
        if stored is None:
            raise AuthorityInvalidError(f"AUTHORITY_INVALID: PolicyReceipt not found for run={run_id} task={task_id}")
        if stored.receipt_id != receipt_id:
            raise AuthorityInvalidError(f"AUTHORITY_INVALID: PolicyReceipt id mismatch {stored.receipt_id} != {receipt_id}")
        expected_digest = compute_policy_receipt_digest(stored)
        if expected_digest != digest:
            raise AuthorityInvalidError("AUTHORITY_INVALID: PolicyReceipt digest mismatch")
        if stored.verdict != PolicyVerdict.ALLOW:
            raise PolicyDeniedError(f"POLICY_DENIED: verdict {stored.verdict}")
        return stored

    def get_provenance(self, run_id: str, task_id: str) -> dict[str, str]:
        if self.supervisor_store is not None:
            try:
                state = self.supervisor_store.load_state(run_id)
                if state and state.repository and state.current_base_sha:
                    return {
                        "repository": state.repository,
                        "canonical_remote": state.canonical_remote or "github",
                        "base_sha": state.current_base_sha,
                    }
            except Exception:
                pass

        prov = self.provenance_store.get(run_id)
        if not prov:
            stored = self.intent_store.get((run_id, task_id))
            if stored and stored.source_repository and stored.source_base_sha:
                prov = {
                    "repository": stored.source_repository,
                    "canonical_remote": "github",
                    "base_sha": stored.source_base_sha,
                }
        if not prov or not prov.get("repository") or not prov.get("canonical_remote") or not prov.get("base_sha"):
            raise SourceProvenanceUnresolvedError("SOURCE_PROVENANCE_UNRESOLVED")
        return dict(prov)

    def get_permitted_capabilities(self, run_id: str, task_id: str) -> Sequence[str] | None:
        return self.permitted_capabilities_store.get((run_id, task_id))

    def _resolve_work_profile_and_policy(self, run_id: str) -> tuple[WorkProfile, EffectivePolicyReport]:
        wp, ep = None, None
        if self.supervisor_store:
            events = self.supervisor_store.load_events(run_id)
            for e in reversed(events):
                if "WORK_PROFILE_BOUND" in str(e.event_type):
                    if "work_profile" in e.payload:
                        from ai_engineering.supervisor.policy.work_profile import validate_work_profile
                        wp = validate_work_profile(e.payload["work_profile"])
                        if e.payload.get("effective_policy"):
                            from ai_engineering.effective_policy import deserialize_effective_policy_report
                            import json
                            ep_dict = e.payload["effective_policy"]
                            if isinstance(ep_dict, dict):
                                ep_str = json.dumps(ep_dict)
                            else:
                                ep_str = str(ep_dict)
                            ep = deserialize_effective_policy_report(ep_str)
                    break
        if not wp or not ep:
            raise PolicyDeniedError(f"POLICY_DENIED: Authority unresolved on cold restart. wp={wp is not None}, ep={ep is not None}, events={len(events) if 'events' in locals() else 0}")

        return wp, ep

    def evaluate_fresh_policy(self, envelope: AgentEnvelope, new_attempt_id: str) -> PolicyReceipt:
        intent = self.resolve_task_intent(envelope.task_intent_id, envelope.task_intent_digest, envelope.run_id, envelope.task_id)
        wp, ep = self._resolve_work_profile_and_policy(envelope.run_id)
        
        a_state, b_state = None, None
        if self.supervisor_store:
            state = self.supervisor_store.load_state(envelope.run_id)
            if state:
                a_state = state.autonomy_state
                b_state = state.budget_state
                
        if not a_state or not b_state:
            raise PolicyDeniedError("POLICY_DENIED: Autonomy or Budget state unresolved")
            
        from ai_engineering.supervisor.state import _autonomy_state_to_dict, _budget_state_to_dict
        from ai_engineering.supervisor.policy.contracts import AutonomyState, AutonomyBudgetState, AutonomyLevel, PolicyRequest, PolicyReceipt, ExecutionTarget
        from ai_engineering.supervisor.policy.engine import evaluate_policy
        ad = _autonomy_state_to_dict(a_state)
        a_state = AutonomyState(
            schema_version=ad.get("schema_version", "hermes.autonomy-state.v1"), run_id=ad["run_id"], profile_id=ad["profile_id"], profile_digest=ad["profile_digest"],
            current_level=AutonomyLevel(ad["current_level"]) if hasattr(AutonomyLevel, ad["current_level"]) else AutonomyLevel.LEVEL_1_LOCAL_WRITE,
            maximum_allowed_level=AutonomyLevel(ad["maximum_allowed_level"]) if hasattr(AutonomyLevel, ad["maximum_allowed_level"]) else AutonomyLevel.LEVEL_1_LOCAL_WRITE,
            successful_runs=ad.get("successful_runs", 0), critical_failures=ad.get("critical_failures", 0), rollback_verified=ad.get("rollback_verified", False),
            required_validators_status=ad.get("required_validators_status", {}), budget_state_digest=ad.get("budget_state_digest", ""),
            promotion_sequence=ad.get("promotion_sequence", 0), last_transition_receipt_id=ad.get("last_transition_receipt_id"),
            created_at_utc=ad.get("created_at_utc", ""), updated_at_utc=ad.get("updated_at_utc", ""), state_digest=ad.get("state_digest", "")
        )
        bd = _budget_state_to_dict(b_state)
        b_state = AutonomyBudgetState(
            schema_version=bd.get("schema_version", "hermes.autonomy-budget-state.v1"), budget_id=bd["budget_id"], budget_digest=bd["budget_digest"],
            decisions_used=bd.get("decisions_used", 0), child_tasks_used=bd.get("child_tasks_used", 0), retries_used=bd.get("retries_used", 0),
            fix_cycles_used=bd.get("fix_cycles_used", 0), consecutive_failures=bd.get("consecutive_failures", 0), provider_calls_used=bd.get("provider_calls_used", 0),
            policy_denials=bd.get("policy_denials", 0), exhausted_dimensions=tuple(bd.get("exhausted_dimensions", ()))
        )
            


        import uuid
        req = PolicyRequest(
            schema_version="hermes.policy-request.v1",
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            attempt_id=new_attempt_id,
            intent_digest=envelope.task_intent_digest,
            decision_id=f"dec-retry-{uuid.uuid4().hex[:6]}",
            decision_receipt_id=f"drec-{uuid.uuid4().hex[:6]}",
            work_profile_id=wp.profile_id,
            work_profile_digest=wp.profile_digest,
            current_autonomy_level=a_state.current_level,
            requested_action="RETRY_OR_FALLBACK",
            requested_effect_classes=(envelope.effect_class,),
            requested_stop_boundary=envelope.stop_boundary,
            execution_target=ExecutionTarget.LOCAL,
            effective_policy_id=ep.effective_policy_id,
            effective_policy_digest=ep.effective_policy_id,
            budget_state_digest=b_state.budget_digest,
        )
        receipt = evaluate_policy(req, intent, ep, wp, a_state, b_state)
        self.receipt_store[(envelope.run_id, envelope.task_id)] = receipt

        if self.supervisor_store:
            events = self.supervisor_store.load_events(envelope.run_id)
            state = self.supervisor_store.load_state(envelope.run_id)
            if events and state:
                from ai_engineering.supervisor.events import create_event, SupervisorEventType
                prev_digest = events[-1].event_digest
                seq = len(events) + 1
                policy_event = create_event(
                    run_id=envelope.run_id,
                    sequence=seq,
                    previous_event_digest=prev_digest,
                    event_type=SupervisorEventType.POLICY_EVALUATED,
                    state_revision=state.state_revision,
                    task_id=envelope.task_id,
                    attempt_id=new_attempt_id,
                    intent_digest=envelope.task_intent_digest,
                    payload={
                        "receipt_id": receipt.receipt_id,
                        "verdict": receipt.verdict.value,
                        "reason_codes": list(receipt.reason_codes),
                        "work_profile_id": wp.profile_id,
                        "current_level": a_state.current_level.value,
                        "policy_receipt": __import__('dataclasses').asdict(receipt),
                    },
                    created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
                )
                self.supervisor_store.save_event(policy_event)

        return receipt

# ── Persistent Store ────────────────────────────────────────────────────────

class PersistentStore:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _safe_path(self, message_id: str) -> str:
        if any(c in message_id for c in ("..", "/", chr(92))):
            raise ValueError("Path traversal detected")
        return os.path.join(self.root_dir, f"{message_id}.json")

    def save(
        self,
        envelope: AgentEnvelope,
        state: str,
        result: Any = None,
        routing_receipt: RoutingReceipt | None = None,
        operation_id: str | None = None,
        failure_class: str | None = None,
    ) -> None:
        file_path = self._safe_path(envelope.message_id)
        history = []
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                    history = existing.get("history", [])
            except Exception:
                pass

        history.append({
            "state": state,
            "timestamp_utc": str(datetime.datetime.now(datetime.timezone.utc)),
            "operation_id": operation_id,
            "failure_class": failure_class,
        })

        data = {
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "causation_id": envelope.causation_id,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "attempt_id": envelope.attempt_id,
            "sender_agent": envelope.sender_agent,
            "recipient_agent": envelope.recipient_agent,
            "recipient_capability": envelope.recipient_capability,
            "message_type": envelope.message_type.value if hasattr(envelope.message_type, "value") else str(envelope.message_type),
            "payload": envelope.payload,
            "payload_digest": envelope.payload_digest,
            "task_intent_id": envelope.task_intent_id,
            "task_intent_digest": envelope.task_intent_digest,
            "policy_receipt_id": envelope.policy_receipt_id,
            "policy_receipt_digest": envelope.policy_receipt_digest,
            "effect_class": envelope.effect_class.name,
            "stop_boundary": envelope.stop_boundary.name,
            "created_at_utc": envelope.created_at_utc,
            "expires_at_utc": envelope.expires_at_utc,
            "retry_count": envelope.retry_count,
            "max_retries": envelope.max_retries,
            "state": state,
            "result": result,
            "operation_id": operation_id,
            "failure_class": failure_class,
            "routing_receipt": __import__("dataclasses").asdict(routing_receipt) if routing_receipt else None,
            "history": history,
        }
        tmp_file = f"{file_path}.tmp.{uuid.uuid4().hex}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_file, file_path)

    def load_envelope(self, message_id: str) -> AgentEnvelope:
        file_path = self._safe_path(message_id)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Message {message_id} not found")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        env = AgentEnvelope(
            schema_version="hermes.agent-envelope.v1",
            message_id=data["message_id"],
            correlation_id=data["correlation_id"],
            causation_id=data.get("causation_id"),
            run_id=data["run_id"],
            task_id=data["task_id"],
            attempt_id=data["attempt_id"],
            sender_agent=data["sender_agent"],
            recipient_agent=data["recipient_agent"],
            recipient_capability=data["recipient_capability"],
            message_type=MessageType(data["message_type"]),
            payload=data["payload"],
            payload_digest=data["payload_digest"],
            task_intent_id=data["task_intent_id"],
            task_intent_digest=data["task_intent_digest"],
            policy_receipt_id=data["policy_receipt_id"],
            policy_receipt_digest=data["policy_receipt_digest"],
            effect_class=EffectClass[data["effect_class"]],
            stop_boundary=StopBoundary[data["stop_boundary"]],
            created_at_utc=data["created_at_utc"],
            expires_at_utc=data.get("expires_at_utc"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 2),
        )
        if not env.verify_integrity():
            raise ReplayMessageInvalidError("REPLAY_MESSAGE_INVALID: persisted payload corrupted or tampered")
        return env

    def load_state(self, message_id: str) -> str:
        file_path = self._safe_path(message_id)
        if not os.path.exists(file_path):
            return "UNKNOWN"
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("state", "UNKNOWN")

    def load_messages(self) -> list[dict[str, Any]]:
        messages = []
        for fname in sorted(os.listdir(self.root_dir)):
            if fname.endswith(".json") and not fname.startswith("stale_") and not fname.startswith("effect_"):
                p = os.path.join(self.root_dir, fname)
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "payload" in data and "payload_digest" in data:
                    self.load_envelope(data["message_id"])
                messages.append(data)
        return messages

    def load_result(self, message_id: str) -> dict[str, Any] | None:
        file_path = self._safe_path(message_id)
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("result")

    def record_stale_evidence(self, envelope: AgentEnvelope, stale_bundle: WorkerResultBundle) -> None:
        p = os.path.join(self.root_dir, f"stale_{envelope.message_id}_{uuid.uuid4().hex[:6]}.json")
        data = {
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "task_id": envelope.task_id,
            "attempt_id": envelope.attempt_id,
            "recorded_at_utc": str(datetime.datetime.now(datetime.timezone.utc)),
            "bundle": json.loads(canonical_serialize_worker_result(stale_bundle)),
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)

    def _effect_receipt_path(self, operation_id: str) -> str:
        safe_id = "".join(c for c in operation_id if c.isalnum() or c in "._-")
        return os.path.join(self.root_dir, f"effect_{safe_id}.json")

    def save_effect_receipt(self, operation_id: str, data: dict[str, Any]) -> None:
        p = self._effect_receipt_path(operation_id)
        tmp = f"{p}.tmp.{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
        os.replace(tmp, p)

    def load_effect_receipt(self, operation_id: str) -> dict[str, Any] | None:
        p = self._effect_receipt_path(operation_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def find_effect_receipt(self, correlation_id: str, attempt_id: str) -> dict[str, Any] | None:
        for fname in os.listdir(self.root_dir):
            if fname.startswith("effect_") and fname.endswith(".json"):
                p = os.path.join(self.root_dir, fname)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("correlation_id") == correlation_id and data.get("attempt_id") == attempt_id:
                            return data
                except Exception:
                    pass
        return None

class FilePersistentStore(PersistentStore):
    pass

# ── Ranks and Evaluation Helpers ────────────────────────────────────────────

def _effect_rank(e: EffectClass) -> int:
    ranks = {
        EffectClass.READ_ONLY: 0,
        EffectClass.REPOSITORY_WRITE: 1,
        EffectClass.GIT_COMMIT: 2,
        EffectClass.GIT_PUSH: 3,
        EffectClass.PR_MUTATION: 4,
        EffectClass.PR_MERGE: 5,
        EffectClass.BUILD: 6,
        EffectClass.DEPLOY: 7,
    }
    return ranks.get(e, 99)

def _stop_rank(s: StopBoundary) -> int:
    return _STOP_BOUNDARY_RANK.get(s, 99)

def _is_effect_allowed(effect: EffectClass, allowed: Any, forbidden: Any) -> bool:
    for f in (forbidden or ()):
        if (isinstance(f, EffectClass) and f == effect) or str(f) in (effect.name, effect.value):
            return False
    if effect == EffectClass.READ_ONLY:
        return True
    for a in (allowed or ()):
        if (isinstance(a, EffectClass) and a == effect) or str(a) in (effect.name, effect.value):
            return True
    return False

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"ROUTED", "BLOCKED"},
    "ROUTED": {"DISPATCHED", "BLOCKED", "CANCEL_REQUESTED", "CANCELLED", "DEFER"},
    "DISPATCHED": {"RUNNING", "FAILED", "TIMED_OUT", "CANCEL_REQUESTED", "CANCELLED", "BLOCKED"},
    "RUNNING": {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCEL_REQUESTED", "BLOCKED"},
    "CANCEL_REQUESTED": {"CANCELLED", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "TIMED_OUT": set(),
    "CANCELLED": set(),
    "BLOCKED": set(),
}

# ── CrossAgentRouter ────────────────────────────────────────────────────────

class CrossAgentRouter:
    def __init__(
        self,
        registry: AgentRegistry,
        authority_resolver: AuthorityResolver,
        store: PersistentStore,
    ) -> None:
        self.registry = registry
        self.authority_resolver = authority_resolver
        self.store = store

    def _transition(
        self,
        envelope: AgentEnvelope,
        new_state: str,
        result: Any = None,
        routing_receipt: RoutingReceipt | None = None,
        operation_id: str | None = None,
        failure_class: str | None = None,
    ) -> None:
        current_state = self.store.load_state(envelope.message_id)
        if current_state != new_state:
            allowed = _VALID_TRANSITIONS.get(current_state, set())
            if new_state not in allowed:
                raise IllegalStateTransitionError(
                    f"ILLEGAL_STATE_TRANSITION: cannot transition from {current_state} to {new_state}"
                )
        self.store.save(
            envelope,
            new_state,
            result=result,
            routing_receipt=routing_receipt,
            operation_id=operation_id,
            failure_class=failure_class,
        )

    def route(
        self,
        envelope: AgentEnvelope,
        caller_provenance: dict[str, str] | None = None,
    ) -> RoutingReceipt:
        if any(c in envelope.message_id for c in ("..", "/", chr(92))):
            raise ValueError("Path traversal detected")

        self.store.save(envelope, "QUEUED")

        if not envelope.verify_integrity():
            self._transition(envelope, "BLOCKED")
            raise MessageTamperedError("AGENT_MESSAGE_PAYLOAD_TAMPERED")

        if not isinstance(envelope.message_type, MessageType):
            self._transition(envelope, "BLOCKED")
            raise UnsupportedMessageTypeError(f"AGENT_MESSAGE_TYPE_UNSUPPORTED: {envelope.message_type}")

        if not envelope.policy_receipt_id or not envelope.policy_receipt_id.strip():
            self._transition(envelope, "BLOCKED")
            raise PolicyDeniedError("POLICY_DENIED: missing policy receipt id")

        # Resolve trusted authority from stores
        intent = self.authority_resolver.resolve_task_intent(
            envelope.task_intent_id, envelope.task_intent_digest, envelope.run_id, envelope.task_id
        )
        receipt = self.authority_resolver.resolve_policy_receipt(
            envelope.policy_receipt_id, envelope.policy_receipt_digest, envelope.run_id, envelope.task_id
        )

        # Validate source provenance binding (trusted vs caller)
        try:
            trusted_prov = self.authority_resolver.get_provenance(envelope.run_id, envelope.task_id)
        except SourceProvenanceUnresolvedError:
            self._transition(envelope, "BLOCKED")
            return RoutingReceipt(
                routing_receipt_id=str(uuid.uuid4()),
                message_id=envelope.message_id,
                correlation_id=envelope.correlation_id,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                requested_capability=envelope.recipient_capability,
                selected_agent=envelope.recipient_agent,
                task_intent_id=envelope.task_intent_id,
                policy_receipt_id=envelope.policy_receipt_id,
                effective_effect_class=envelope.effect_class.name,
                effective_stop_boundary=envelope.stop_boundary.name,
                decision="BLOCK",
                reason="SOURCE_PROVENANCE_UNRESOLVED",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
            )
        if caller_provenance is not None:
            for k in ("repository", "canonical_remote", "base_sha"):
                if k in caller_provenance and caller_provenance[k] != trusted_prov.get(k):
                    self._transition(envelope, "BLOCKED")
                    raise SourceProvenanceMismatchError(
                        f"SOURCE_PROVENANCE_MISMATCH: caller {k}={caller_provenance[k]} != trusted {trusted_prov.get(k)}"
                    )
        if isinstance(envelope.payload, dict) and "repository" in envelope.payload:
            if envelope.payload["repository"] != trusted_prov["repository"]:
                self._transition(envelope, "BLOCKED")
                raise SourceProvenanceMismatchError(
                    f"SOURCE_PROVENANCE_MISMATCH: payload repository {envelope.payload['repository']} != trusted {trusted_prov['repository']}"
                )

        # 1. Independent EffectClass verification (must be allowed and not forbidden)
        if not _is_effect_allowed(envelope.effect_class, intent.allowed_mutations, intent.forbidden_mutations):
            self._transition(envelope, "BLOCKED")
            raise EffectClassEscalationError("EFFECT_CLASS_ESCALATION")

        # 2. Independent StopBoundary verification
        if _stop_rank(envelope.stop_boundary) > _stop_rank(intent.stop_boundary):
            self._transition(envelope, "BLOCKED")
            raise StopBoundaryEscalationError("STOP_BOUNDARY_ESCALATION")

        adapter = self.registry.get_adapter(envelope.recipient_agent)
        definition = self.registry.get_definition(envelope.recipient_agent)

        if not adapter or not definition:
            self._transition(envelope, "BLOCKED")
            return RoutingReceipt(
                routing_receipt_id=str(uuid.uuid4()),
                message_id=envelope.message_id,
                correlation_id=envelope.correlation_id,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                requested_capability=envelope.recipient_capability,
                selected_agent=envelope.recipient_agent,
                task_intent_id=envelope.task_intent_id,
                policy_receipt_id=envelope.policy_receipt_id,
                effective_effect_class=envelope.effect_class.name,
                effective_stop_boundary=envelope.stop_boundary.name,
                decision="BLOCK",
                reason="UNKNOWN_AGENT",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
            )

        if not adapter.health():
            self._transition(envelope, "ROUTED")
            receipt_defer = RoutingReceipt(
                routing_receipt_id=str(uuid.uuid4()),
                message_id=envelope.message_id,
                correlation_id=envelope.correlation_id,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                requested_capability=envelope.recipient_capability,
                selected_agent=envelope.recipient_agent,
                task_intent_id=envelope.task_intent_id,
                policy_receipt_id=envelope.policy_receipt_id,
                effective_effect_class=envelope.effect_class.name,
                effective_stop_boundary=envelope.stop_boundary.name,
                decision="DEFER",
                reason="WORKER_UNAVAILABLE",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
            )
            self._transition(envelope, "DEFER", routing_receipt=receipt_defer)
            return receipt_defer

        # Check permitted capabilities if configured in authority
        permitted_caps = self.authority_resolver.get_permitted_capabilities(envelope.run_id, envelope.task_id)
        if permitted_caps is not None:
            if envelope.recipient_capability not in permitted_caps:
                self._transition(envelope, "BLOCKED")
                return RoutingReceipt(
                    routing_receipt_id=str(uuid.uuid4()),
                    message_id=envelope.message_id,
                    correlation_id=envelope.correlation_id,
                    run_id=envelope.run_id,
                    task_id=envelope.task_id,
                    attempt_id=envelope.attempt_id,
                    requested_capability=envelope.recipient_capability,
                    selected_agent=envelope.recipient_agent,
                    task_intent_id=envelope.task_intent_id,
                    policy_receipt_id=envelope.policy_receipt_id,
                    effective_effect_class=envelope.effect_class.name,
                    effective_stop_boundary=envelope.stop_boundary.name,
                    decision="BLOCK",
                    reason=f"CAPABILITY_NOT_AUTHORIZED: capability {envelope.recipient_capability} denied by PolicyReceipt",
                    attempt_number=envelope.retry_count,
                    created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
                )

        if envelope.recipient_capability not in definition.capabilities:
            self._transition(envelope, "BLOCKED")
            return RoutingReceipt(
                routing_receipt_id=str(uuid.uuid4()),
                message_id=envelope.message_id,
                correlation_id=envelope.correlation_id,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                requested_capability=envelope.recipient_capability,
                selected_agent=envelope.recipient_agent,
                task_intent_id=envelope.task_intent_id,
                policy_receipt_id=envelope.policy_receipt_id,
                effective_effect_class=envelope.effect_class.name,
                effective_stop_boundary=envelope.stop_boundary.name,
                decision="BLOCK",
                reason=f"CAPABILITY_NOT_AUTHORIZED: capability {envelope.recipient_capability} not in agent capabilities",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
            )

        if envelope.recipient_capability not in intent.allowed_mutations:
            self._transition(envelope, "BLOCKED")
            return RoutingReceipt(
                routing_receipt_id=str(uuid.uuid4()),
                message_id=envelope.message_id,
                correlation_id=envelope.correlation_id,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                requested_capability=envelope.recipient_capability,
                selected_agent=envelope.recipient_agent,
                task_intent_id=envelope.task_intent_id,
                policy_receipt_id=envelope.policy_receipt_id,
                effective_effect_class=envelope.effect_class.name,
                effective_stop_boundary=envelope.stop_boundary.name,
                decision="BLOCK",
                reason=f"CAPABILITY_NOT_AUTHORIZED: capability {envelope.recipient_capability} denied by TaskIntent",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
            )

        if envelope.message_type not in definition.supported_message_types:
            self._transition(envelope, "BLOCKED")
            raise UnsupportedMessageTypeError(
                f"AGENT_MESSAGE_TYPE_UNSUPPORTED: {envelope.message_type} not supported by {envelope.recipient_agent}"
            )

        receipt = RoutingReceipt(
            routing_receipt_id=str(uuid.uuid4()),
            message_id=envelope.message_id,
            correlation_id=envelope.correlation_id,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            attempt_id=envelope.attempt_id,
            requested_capability=envelope.recipient_capability,
            selected_agent=envelope.recipient_agent,
            task_intent_id=envelope.task_intent_id,
            policy_receipt_id=envelope.policy_receipt_id,
            effective_effect_class=envelope.effect_class.name,
            effective_stop_boundary=envelope.stop_boundary.name,
            decision="ROUTE",
            reason="AUTHORIZED",
            attempt_number=envelope.retry_count,
            created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
        )
        self._transition(envelope, "ROUTED", routing_receipt=receipt)
        return receipt

    async def dispatch(
        self,
        envelope: AgentEnvelope,
        fallback_candidate: str | None = None,
        caller_provenance: dict[str, str] | None = None,
    ) -> WorkerResultBundle:
        # Idempotency check 1: if already SUCCEEDED, return cached result
        if self.store.load_state(envelope.message_id) == "SUCCEEDED":
            cached_res = self.store.load_result(envelope.message_id)
            if cached_res:
                return deserialize_worker_result(json.dumps(cached_res))

        # Idempotency check 2: crash-safe replay reconciliation
        # If worker effect succeeded and receipt exists, recover without re-executing effect!
        existing_effect = self.store.find_effect_receipt(envelope.correlation_id, envelope.attempt_id)
        if existing_effect and existing_effect.get("status") == "COMMITTED" and "result" in existing_effect:
            res_bundle = deserialize_worker_result(json.dumps(existing_effect["result"]))
            self._transition(
                envelope,
                "SUCCEEDED",
                result=existing_effect["result"],
                operation_id=existing_effect.get("operation_id"),
            )
            return res_bundle

        receipt = self.route(envelope, caller_provenance=caller_provenance)

        # Handle fallback if primary is unavailable (DEFER)
        if receipt.decision == "DEFER" and fallback_candidate:
            cand_def = self.registry.get_definition(fallback_candidate)
            cand_adapter = self.registry.get_adapter(fallback_candidate)
            if cand_def and cand_adapter and cand_adapter.health():
                # Fresh authority check before fallback dispatch
                self.authority_resolver.resolve_task_intent(
                    envelope.task_intent_id, envelope.task_intent_digest, envelope.run_id, envelope.task_id
                )
                self.authority_resolver.resolve_policy_receipt(
                    envelope.policy_receipt_id, envelope.policy_receipt_digest, envelope.run_id, envelope.task_id
                )
                if envelope.recipient_capability not in cand_def.capabilities:
                    raise CapabilityNotAuthorizedError(
                        f"CAPABILITY_NOT_AUTHORIZED: {envelope.recipient_capability} not in {cand_def.capabilities}"
                    )
                new_attempt_id = f"att-fallback-{uuid.uuid4().hex[:6]}"
                
                new_receipt = self.authority_resolver.evaluate_fresh_policy(envelope, new_attempt_id)
                if new_receipt.verdict != PolicyVerdict.ALLOW:
                    raise PolicyDeniedError("POLICY_DENIED: fallback policy evaluation failed")

                fallback_env = replace(
                    envelope,
                    message_id=str(uuid.uuid4()),
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.message_id,
                    attempt_id=new_attempt_id,
                    recipient_agent=fallback_candidate,
                    retry_count=envelope.retry_count + 1,
                    policy_receipt_id=new_receipt.receipt_id,
                    policy_receipt_digest=compute_policy_receipt_digest(new_receipt),
                    created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
                )
                from .envelope import compute_payload_digest
                fallback_env = replace(fallback_env, payload_digest=compute_payload_digest(fallback_env.payload))
                return await self.dispatch(fallback_env, caller_provenance=caller_provenance)

        if receipt.decision != "ROUTE":
            if receipt.reason.startswith("CAPABILITY_NOT_AUTHORIZED"):
                raise CapabilityNotAuthorizedError(receipt.reason)
            if receipt.reason.startswith("SOURCE_PROVENANCE_UNRESOLVED"):
                raise SourceProvenanceUnresolvedError(receipt.reason)
            raise RouterError(receipt.reason)

        adapter = self.registry.get_adapter(envelope.recipient_agent)
        definition = self.registry.get_definition(envelope.recipient_agent)
        provenance = self.authority_resolver.get_provenance(envelope.run_id, envelope.task_id)

        operation_id = f"op-{envelope.run_id}-{envelope.attempt_id}-{uuid.uuid4().hex[:6]}"
        self._transition(envelope, "DISPATCHED", operation_id=operation_id)
        self._transition(envelope, "RUNNING", operation_id=operation_id)

        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(adapter.dispatch, envelope, definition.timeout_seconds, provenance, operation_id),
                timeout=float(definition.timeout_seconds),
            )
        except asyncio.TimeoutError:
            cancel_confirmed = adapter.cancel(operation_id)
            is_running = getattr(adapter, "is_running", lambda op: False)(operation_id)
            if cancel_confirmed is False or is_running:
                self._transition(
                    envelope,
                    "BLOCKED",
                    operation_id=operation_id,
                    failure_class="TIMEOUT_CANCELLATION_UNCONFIRMED",
                )
                raise TimeoutCancellationUnconfirmedError("TIMEOUT_CANCELLATION_UNCONFIRMED")

            self._transition(envelope, "TIMED_OUT", operation_id=operation_id, failure_class="TIMEOUT")
            raise asyncio.TimeoutError("WORK_TIMEOUT")
        except Exception as exc:
            if isinstance(exc, tuple(NON_RETRYABLE_EXCEPTIONS)):
                self._transition(envelope, "FAILED", operation_id=operation_id, failure_class=type(exc).__name__)
                raise

            # Retry classification and budget check
            if envelope.retry_count < envelope.max_retries:
                # Fresh authority recheck before retry
                self.authority_resolver.resolve_task_intent(
                    envelope.task_intent_id, envelope.task_intent_digest, envelope.run_id, envelope.task_id
                )
                self.authority_resolver.resolve_policy_receipt(
                    envelope.policy_receipt_id, envelope.policy_receipt_digest, envelope.run_id, envelope.task_id
                )
                new_attempt_id = f"{envelope.attempt_id}-retry-{envelope.retry_count + 1}"
                
                new_receipt = self.authority_resolver.evaluate_fresh_policy(envelope, new_attempt_id)
                if new_receipt.verdict != PolicyVerdict.ALLOW:
                    raise PolicyDeniedError("POLICY_DENIED: retry policy evaluation failed")

                retry_env = replace(
                    envelope,
                    message_id=str(uuid.uuid4()),
                    causation_id=envelope.message_id,
                    attempt_id=new_attempt_id,
                    retry_count=envelope.retry_count + 1,
                    policy_receipt_id=new_receipt.receipt_id,
                    policy_receipt_digest=compute_policy_receipt_digest(new_receipt),
                    created_at_utc=str(datetime.datetime.now(datetime.timezone.utc)),
                )
                from .envelope import compute_payload_digest
                retry_env = replace(retry_env, payload_digest=compute_payload_digest(retry_env.payload))
                return await self.dispatch(
                    retry_env,
                    fallback_candidate=fallback_candidate,
                    caller_provenance=caller_provenance,
                )

            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class=type(exc).__name__)
            raise

        # Stale attempt check
        current_state = self.store.load_state(envelope.message_id)
        if current_state in ("TIMED_OUT", "CANCELLED", "FAILED", "BLOCKED"):
            self.store.record_stale_evidence(envelope, res)
            raise StaleResultError(f"STALE_RESULT: attempt was terminated with state {current_state}")

        # Save effect receipt immediately after worker returns to guarantee crash safety
        effect_data = {
            "operation_id": operation_id,
            "correlation_id": envelope.correlation_id,
            "attempt_id": envelope.attempt_id,
            "status": "COMMITTED",
            "result": json.loads(canonical_serialize_worker_result(res)),
        }
        self.store.save_effect_receipt(operation_id, effect_data)

        # Strict result identity validation
        if res.task_id != envelope.task_id:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class="WORKER_RESULT_IDENTITY_MISMATCH")
            raise WorkerResultIdentityMismatchError(f"WORKER_RESULT_IDENTITY_MISMATCH: task_id {res.task_id} != {envelope.task_id}")
        if res.attempt_id != envelope.attempt_id:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class="WORKER_RESULT_IDENTITY_MISMATCH")
            raise WorkerResultIdentityMismatchError(f"WORKER_RESULT_IDENTITY_MISMATCH: attempt_id {res.attempt_id} != {envelope.attempt_id}")
        if res.intent_digest != envelope.task_intent_digest:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class="WORKER_RESULT_IDENTITY_MISMATCH")
            raise WorkerResultIdentityMismatchError("WORKER_RESULT_IDENTITY_MISMATCH: intent_digest mismatch")
        if res.worker_id != envelope.recipient_agent:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class="WORKER_RESULT_IDENTITY_MISMATCH")
            raise WorkerResultIdentityMismatchError(f"WORKER_RESULT_IDENTITY_MISMATCH: worker_id {res.worker_id} != {envelope.recipient_agent}")
        if res.repository != provenance["repository"]:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class="WORKER_RESULT_IDENTITY_MISMATCH")
            raise WorkerResultIdentityMismatchError(f"WORKER_RESULT_IDENTITY_MISMATCH: repository {res.repository} != {provenance['repository']}")
        if res.base_sha != provenance["base_sha"]:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class="WORKER_RESULT_IDENTITY_MISMATCH")
            raise WorkerResultIdentityMismatchError(f"WORKER_RESULT_IDENTITY_MISMATCH: base_sha {res.base_sha} != {provenance['base_sha']}")

        res_dict = json.loads(canonical_serialize_worker_result(res))
        self._transition(envelope, "SUCCEEDED", result=res_dict, operation_id=operation_id)
        return res

    def cancel(self, run_id: str, task_id: str, attempt_id: str, correlation_id: str) -> None:
        messages = self.store.load_messages()
        matching = [m for m in messages if m.get("correlation_id") == correlation_id]
        if not matching:
            raise StaleOrForeignAttemptError(f"STALE_OR_FOREIGN_ATTEMPT: correlation_id {correlation_id} not found")

        for m in matching:
            if m.get("run_id") != run_id:
                raise CrossRunViolationError(f"CROSS_RUN_CANCELLATION: run_id {run_id} does not match message run {m.get('run_id')}")
            if m.get("task_id") != task_id or m.get("attempt_id") != attempt_id:
                raise StaleOrForeignAttemptError("STALE_OR_FOREIGN_ATTEMPT: attempt mismatch")

            env = self.store.load_envelope(m["message_id"])
            op_id = m.get("operation_id") or correlation_id
            adapter = self.registry.get_adapter(env.recipient_agent)

            self._transition(env, "CANCEL_REQUESTED", operation_id=op_id)
            if adapter:
                adapter.cancel(op_id)
            self._transition(env, "CANCELLED", operation_id=op_id)
