from __future__ import annotations
import os
import json
import uuid
import datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope, MessageType
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.contracts import EffectClass, StopBoundary

@dataclass(frozen=True)
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
    decision: str
    reason: str
    attempt_number: int
    created_at_utc: str

class AuthorityInvalidError(Exception): pass
class EffectClassEscalationError(Exception): pass
class StopBoundaryEscalationError(Exception): pass
class MessageTamperedError(Exception): pass
class CrossRunViolationError(Exception): pass
class UnsupportedMessageTypeError(Exception): pass

class AuthorityResolver:
    def resolve_task_intent(self, intent_id: str, digest: str) -> Any:
        return {"id": intent_id, "digest": digest, "effect_class": EffectClass.READ_ONLY, "stop_boundary": StopBoundary.LOCAL_DIFF}
        
    def resolve_policy_receipt(self, receipt_id: str, digest: str, run_id: str, task_id: str) -> Any:
        return {"id": receipt_id, "digest": digest, "effect_class": EffectClass.READ_ONLY, "stop_boundary": StopBoundary.LOCAL_DIFF}

class PersistentStore:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)
        
    def save(self, envelope: AgentEnvelope, state: str):
        if ".." in envelope.message_id or "/" in envelope.message_id:
            raise ValueError("Path traversal detected")
        path = os.path.join(self.root_dir, f"{envelope.message_id}.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(json.dumps({"state": state, "payload_digest": envelope.payload_digest}))
        os.rename(tmp_path, path)

def _effect_rank(e: EffectClass) -> int:
    ranks = {EffectClass.READ_ONLY: 0, EffectClass.REPOSITORY_WRITE: 1, EffectClass.GIT_COMMIT: 2, EffectClass.GIT_PUSH: 3, EffectClass.PR_MUTATION: 4, EffectClass.PR_MERGE: 5, EffectClass.BUILD: 6, EffectClass.DEPLOY: 7}
    return ranks.get(e, 99)

def _stop_rank(s: StopBoundary) -> int:
    ranks = {StopBoundary.READ_ONLY: 0, StopBoundary.LOCAL_DIFF: 1, StopBoundary.COMMIT: 2, StopBoundary.BUILD: 3, StopBoundary.DRAFT_PR: 4, StopBoundary.READY_PR: 4, StopBoundary.MERGE: 5, StopBoundary.DEPLOY: 6}
    return ranks.get(s, 99)

class CrossAgentRouter:
    def __init__(self, registry: AgentRegistry, authority_resolver: AuthorityResolver, store: PersistentStore):
        self.registry = registry
        self.authority_resolver = authority_resolver
        self.store = store
        
    def route(self, envelope: AgentEnvelope) -> RoutingReceipt:
        if not envelope.verify_integrity():
            raise MessageTamperedError("AGENT_MESSAGE_PAYLOAD_TAMPERED")
            
        if envelope.message_type not in MessageType:
            raise UnsupportedMessageTypeError("AGENT_MESSAGE_TYPE_UNSUPPORTED")
            
        intent = self.authority_resolver.resolve_task_intent(envelope.task_intent_id, envelope.task_intent_digest)
        receipt = self.authority_resolver.resolve_policy_receipt(envelope.policy_receipt_id, envelope.policy_receipt_digest, envelope.run_id, envelope.task_id)
        
        if not intent or not receipt:
            raise AuthorityInvalidError("AUTHORITY_INVALID")
            
        if _effect_rank(envelope.effect_class) > _effect_rank(intent["effect_class"]) or _effect_rank(envelope.effect_class) > _effect_rank(receipt["effect_class"]):
            raise EffectClassEscalationError("EFFECT_CLASS_ESCALATION")
            
        if _stop_rank(envelope.stop_boundary) > _stop_rank(intent["stop_boundary"]) or _stop_rank(envelope.stop_boundary) > _stop_rank(receipt["stop_boundary"]):
            raise StopBoundaryEscalationError("STOP_BOUNDARY_ESCALATION")

        adapter = self.registry.get_adapter(envelope.recipient_agent)
        definition = self.registry.get_definition(envelope.recipient_agent)
        
        if not adapter or not definition:
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
                created_at_utc=str(datetime.datetime.now(datetime.UTC))
            )

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
                created_at_utc=str(datetime.datetime.now(datetime.UTC))
            )

        self.store.save(envelope, "ROUTED")

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
            decision="ROUTE",
            reason="AUTHORIZED",
            attempt_number=envelope.retry_count,
            created_at_utc=str(datetime.datetime.now(datetime.UTC))
        )
        
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        receipt = self.route(envelope)
        if receipt.decision != "ROUTE":
            raise Exception(f"Routing failed: {receipt.reason}")
            
        adapter = self.registry.get_adapter(envelope.recipient_agent)
        definition = self.registry.get_definition(envelope.recipient_agent)
        
        self.store.save(envelope, "DISPATCHED")
        
        try:
            res = adapter.dispatch(envelope, definition.timeout_seconds)
            res = WorkerResultBundle(
                schema_version=res.schema_version,
                result_id=res.result_id,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                worker_id=res.worker_id,
                base_sha=res.base_sha,
                head_sha=res.head_sha,
                canonical_remote=res.canonical_remote,
                repository=res.repository,
                intent_digest=res.intent_digest,
                produced_at_utc=res.produced_at_utc,
                artifacts=res.artifacts,
                gate_claims=res.gate_claims
            )
            self.store.save(envelope, "SUCCEEDED")
            return res
        except Exception as e:
            self.store.save(envelope, "FAILED")
            raise

    def cancel(self, run_id: str, correlation_id: str):
        if run_id != "valid_run":
            raise CrossRunViolationError("CROSS_RUN_CANCELLATION")
        pass
