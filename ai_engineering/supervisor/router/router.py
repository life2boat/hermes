from __future__ import annotations
import os
import json
import uuid
import datetime
import asyncio
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope, MessageType, compute_payload_digest
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.supervisor.router.adapters import (
    PolicyDeniedError,
)
from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.control_plane.orchestrator import _STOP_BOUNDARY_RANK
from ai_engineering.task_intent import TaskIntent, intent_digest
from ai_engineering.supervisor.policy.contracts import PolicyReceipt, PolicyVerdict, compute_deterministic_digest

# ── Exceptions ─────────────────────────────────────────────────────────────

class RouterError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)

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
class ReplayMessageInvalidError(RouterError): pass
class StaleOrForeignAttemptError(RouterError): pass

NON_RETRYABLE_EXCEPTIONS = (
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
    ReplayMessageInvalidError,
    StaleOrForeignAttemptError,
    IllegalStateTransitionError,
)

# ── Routing Receipt ────────────────────────────────────────────────────────

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

# ── Authority Resolver ──────────────────────────────────────────────────────

def compute_policy_receipt_digest(receipt: PolicyReceipt) -> str:
    data = {
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
        "reason_codes": list(receipt.reason_codes),
        "created_at_utc": receipt.created_at_utc,
    }
    return compute_deterministic_digest(data)

class AuthorityResolver:
    def __init__(
        self,
        intent_store: Mapping[tuple[str, str], TaskIntent] | None = None,
        receipt_store: Mapping[tuple[str, str], PolicyReceipt] | None = None,
        provenance_store: Mapping[str, Mapping[str, str]] | None = None,
        permitted_capabilities_store: Mapping[tuple[str, str], Sequence[str]] | None = None,
    ) -> None:
        self.intent_store = dict(intent_store or {})
        self.receipt_store = dict(receipt_store or {})
        self.provenance_store = dict(provenance_store or {})
        self.permitted_capabilities_store = dict(permitted_capabilities_store or {})

    def register_authority(
        self,
        run_id: str,
        task_id: str,
        intent: TaskIntent,
        receipt: PolicyReceipt,
        provenance: Mapping[str, str] | None = None,
        permitted_capabilities: Sequence[str] | None = None,
    ) -> None:
        self.intent_store[(run_id, task_id)] = intent
        self.receipt_store[(run_id, task_id)] = receipt
        if provenance:
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
        stored = self.intent_store.get((run_id, task_id))
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

# ── Persistent Store ────────────────────────────────────────────────────────

class PersistentStore:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)

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
        path = self._safe_path(envelope.message_id)
        tmp_path = path + ".tmp"

        existing_data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as rf:
                    existing_data = json.load(rf)
            except Exception:
                pass

        now_utc = str(datetime.datetime.now(datetime.UTC))
        data = {
            "schema_version": "hermes.agent-persistent-record.v1",
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
            "effect_class": envelope.effect_class.value if hasattr(envelope.effect_class, "value") else str(envelope.effect_class),
            "stop_boundary": envelope.stop_boundary.value if hasattr(envelope.stop_boundary, "value") else str(envelope.stop_boundary),
            "state": state,
            "created_at": existing_data.get("created_at", envelope.created_at_utc),
            "created_at_utc": envelope.created_at_utc,
            "expires_at_utc": envelope.expires_at_utc,
            "updated_at": now_utc,
            "operation_id": operation_id or existing_data.get("operation_id"),
            "attempt_number": envelope.retry_count,
            "retry_budget": max(0, envelope.max_retries - envelope.retry_count),
            "failure_class": failure_class or existing_data.get("failure_class"),
            "result": result if result is not None else existing_data.get("result"),
            "routing_receipt": routing_receipt.routing_receipt_id if routing_receipt else existing_data.get("routing_receipt"),
            "stale_evidence": existing_data.get("stale_evidence", []),
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp_path, path)

    def load_envelope(self, message_id: str) -> AgentEnvelope:
        path = self._safe_path(message_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Message {message_id} not found")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        payload = data["payload"]
        if compute_payload_digest(payload) != data["payload_digest"]:
            raise ReplayMessageInvalidError(f"REPLAY_MESSAGE_INVALID: payload tampered for {message_id}")

        return AgentEnvelope.from_dict(data)

    def load_state(self, message_id: str) -> str:
        path = self._safe_path(message_id)
        if not os.path.exists(path):
            return "QUEUED"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("state", "QUEUED")

    def load_messages(self) -> list[dict[str, Any]]:
        msgs = []
        for f in os.listdir(self.root_dir):
            if f.endswith(".json") and not f.endswith(".tmp"):
                file_path = os.path.join(self.root_dir, f)
                with open(file_path, "r", encoding="utf-8") as fd:
                    data = json.load(fd)
                    payload = data.get("payload", {})
                    if compute_payload_digest(payload) != data.get("payload_digest"):
                        raise ReplayMessageInvalidError(f"REPLAY_MESSAGE_INVALID: tampered file {f}")
                    msgs.append(data)
        return msgs

    def load_result(self, message_id: str) -> dict[str, Any] | None:
        path = self._safe_path(message_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("result")

    def record_stale_evidence(self, envelope: AgentEnvelope, stale_bundle: WorkerResultBundle) -> None:
        path = self._safe_path(envelope.message_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stale_list = data.get("stale_evidence", [])
            stale_list.append({
                "result_id": stale_bundle.result_id,
                "attempt_id": stale_bundle.attempt_id,
                "recorded_at_utc": str(datetime.datetime.now(datetime.UTC)),
            })
            data["stale_evidence"] = stale_list
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp_path, path)

FilePersistentStore = PersistentStore

# ── Ranks ──────────────────────────────────────────────────────────────────

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

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"ROUTED", "BLOCKED"},
    "ROUTED": {"DISPATCHED", "BLOCKED", "CANCEL_REQUESTED", "CANCELLED", "DEFER"},
    "DISPATCHED": {"RUNNING", "FAILED", "TIMED_OUT", "CANCEL_REQUESTED", "CANCELLED"},
    "RUNNING": {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCEL_REQUESTED"},
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

    def route(self, envelope: AgentEnvelope) -> RoutingReceipt:
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

        # Resolve trusted authority from stores (fails closed on mismatch or forged references)
        intent = self.authority_resolver.resolve_task_intent(
            envelope.task_intent_id, envelope.task_intent_digest, envelope.run_id, envelope.task_id
        )
        receipt = self.authority_resolver.resolve_policy_receipt(
            envelope.policy_receipt_id, envelope.policy_receipt_digest, envelope.run_id, envelope.task_id
        )

        # Check effect class escalation
        if _effect_rank(envelope.effect_class) > _stop_rank(intent.stop_boundary):
            self._transition(envelope, "BLOCKED")
            raise EffectClassEscalationError("EFFECT_CLASS_ESCALATION")

        # Check stop boundary escalation
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
                created_at_utc=str(datetime.datetime.now(datetime.UTC)),
            )

        # Real capability-policy intersection:
        # requested ∩ agent.capabilities ∩ TaskIntent.allowed_mutations ∩ PolicyReceipt permitted capabilities
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
                reason="CAPABILITY_NOT_AUTHORIZED: not in agent capabilities",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.UTC)),
            )

        # Check intent capabilities
        if hasattr(intent, "allowed_mutations") and intent.allowed_mutations:
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
                    reason="CAPABILITY_NOT_AUTHORIZED: denied by TaskIntent",
                    attempt_number=envelope.retry_count,
                    created_at_utc=str(datetime.datetime.now(datetime.UTC)),
                )

        # Check policy receipt permitted capabilities
        permitted_caps = self.authority_resolver.get_permitted_capabilities(envelope.run_id, envelope.task_id)
        if permitted_caps is not None and envelope.recipient_capability not in permitted_caps:
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
                reason="CAPABILITY_NOT_AUTHORIZED: denied by PolicyReceipt",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.UTC)),
            )

        # Check dynamic source provenance
        try:
            self.authority_resolver.get_provenance(envelope.run_id, envelope.task_id)
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
                created_at_utc=str(datetime.datetime.now(datetime.UTC)),
            )

        # Health check
        if not adapter.health():
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
                decision="DEFER",
                reason="WORKER_UNAVAILABLE",
                attempt_number=envelope.retry_count,
                created_at_utc=str(datetime.datetime.now(datetime.UTC)),
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
            created_at_utc=str(datetime.datetime.now(datetime.UTC)),
        )
        self._transition(envelope, "ROUTED", routing_receipt=receipt)
        return receipt

    async def dispatch(
        self,
        envelope: AgentEnvelope,
        fallback_candidate: str | None = None,
    ) -> WorkerResultBundle:
        # Idempotency check: if already SUCCEEDED, return cached result
        if self.store.load_state(envelope.message_id) == "SUCCEEDED":
            cached_res = self.store.load_result(envelope.message_id)
            if cached_res:
                from ai_engineering.supervisor.worker_result import deserialize_worker_result
                return deserialize_worker_result(json.dumps(cached_res))

        receipt = self.route(envelope)

        # Handle fallback if worker is unavailable
        if receipt.decision == "DEFER" and fallback_candidate:
            cand_def = self.registry.get_definition(fallback_candidate)
            cand_adapter = self.registry.get_adapter(fallback_candidate)
            if cand_def and cand_adapter and cand_adapter.health():
                new_attempt_id = f"att-fallback-{uuid.uuid4().hex[:6]}"
                fallback_env = AgentEnvelope(
                    schema_version=envelope.schema_version,
                    message_id=str(uuid.uuid4()),
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.message_id,
                    run_id=envelope.run_id,
                    task_id=envelope.task_id,
                    attempt_id=new_attempt_id,
                    sender_agent=envelope.sender_agent,
                    recipient_agent=fallback_candidate,
                    recipient_capability=envelope.recipient_capability,
                    message_type=envelope.message_type,
                    payload=envelope.payload,
                    payload_digest=envelope.payload_digest,
                    task_intent_id=envelope.task_intent_id,
                    task_intent_digest=envelope.task_intent_digest,
                    policy_receipt_id=envelope.policy_receipt_id,
                    policy_receipt_digest=envelope.policy_receipt_digest,
                    effect_class=envelope.effect_class,
                    stop_boundary=envelope.stop_boundary,
                    created_at_utc=str(datetime.datetime.now(datetime.UTC)),
                    expires_at_utc=envelope.expires_at_utc,
                    retry_count=envelope.retry_count + 1,
                    max_retries=envelope.max_retries,
                )
                return await self.dispatch(fallback_env)

        if receipt.decision != "ROUTE":
            if receipt.reason.startswith("CAPABILITY_NOT_AUTHORIZED"):
                raise CapabilityNotAuthorizedError(receipt.reason)
            raise RouterError(receipt.reason)

        adapter = self.registry.get_adapter(envelope.recipient_agent)
        definition = self.registry.get_definition(envelope.recipient_agent)
        provenance = self.authority_resolver.get_provenance(envelope.run_id, envelope.task_id)

        operation_id = f"op-{envelope.run_id}-{envelope.attempt_id}-{uuid.uuid4().hex[:6]}"
        self._transition(envelope, "DISPATCHED", operation_id=operation_id)
        self._transition(envelope, "RUNNING", operation_id=operation_id)

        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(adapter.dispatch, envelope, definition.timeout_seconds, provenance),
                timeout=float(definition.timeout_seconds),
            )
        except asyncio.TimeoutError:
            self._transition(envelope, "TIMED_OUT", operation_id=operation_id, failure_class="TIMEOUT")
            adapter.cancel(operation_id)
            raise asyncio.TimeoutError("WORK_TIMEOUT")
        except Exception as exc:
            self._transition(envelope, "FAILED", operation_id=operation_id, failure_class=type(exc).__name__)
            raise

        # Check if attempt timed out or was terminated while dispatching (stale result)
        current_state = self.store.load_state(envelope.message_id)
        if current_state in ("TIMED_OUT", "CANCELLED", "FAILED"):
            self.store.record_stale_evidence(envelope, res)
            raise StaleResultError(f"STALE_RESULT: attempt was terminated with state {current_state}")

        # Strict result identity validation (Section 13)
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

        from ai_engineering.supervisor.worker_result import canonical_serialize_worker_result
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
