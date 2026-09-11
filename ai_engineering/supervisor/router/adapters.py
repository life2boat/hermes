from __future__ import annotations
from typing import Protocol, Mapping, Any, Optional
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope

class AgentTransportUnavailableError(Exception):
    pass

class PolicyDeniedError(Exception):
    pass

class AgentTransport(Protocol):
    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle: ...
    def cancel(self, operation_id: str) -> None: ...
    def health(self) -> bool: ...

class AgentAdapter(Protocol):
    def dispatch(self, envelope: AgentEnvelope, timeout: int) -> WorkerResultBundle: ...
    def cancel(self, correlation_id: str) -> None: ...
    def health(self) -> bool: ...

class BaseAgentAdapter:
    def __init__(self, transport: Optional[AgentTransport] = None):
        self.transport = transport
        
    def dispatch(self, envelope: AgentEnvelope, timeout: int) -> WorkerResultBundle:
        if not self.transport or not self.transport.health():
            raise AgentTransportUnavailableError(f"AGENT_TRANSPORT_UNAVAILABLE")
        
        request = {
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "attempt_id": envelope.attempt_id,
            "repository": "life2boat/hermes",
            "canonical_remote": "github",
            "base_sha": "5bf77a9fde5caf9435fb9f61820f5c09b23258c5",
            "objective": envelope.payload.get("objective", ""),
            "allowed_scope": envelope.payload.get("allowed_scope", []),
            "task_intent_id": envelope.task_intent_id,
            "task_intent_digest": envelope.task_intent_digest,
            "policy_receipt_id": envelope.policy_receipt_id,
            "policy_receipt_digest": envelope.policy_receipt_digest,
            "effective_effect_class": envelope.effect_class.name,
            "effective_stop_boundary": envelope.stop_boundary.name,
            "required_validators": [],
            "result_schema": "hermes.worker-result.v1"
        }
        return self.transport.dispatch(request, timeout)
        
    def cancel(self, correlation_id: str) -> None:
        if self.transport:
            self.transport.cancel(correlation_id)
            
    def health(self) -> bool:
        return self.transport is not None and self.transport.health()

class AntigravityAdapter(BaseAgentAdapter): pass
class CodexAdapter(BaseAgentAdapter): pass

class ComputerUseAdapter(BaseAgentAdapter):
    def dispatch(self, envelope: AgentEnvelope, timeout: int) -> WorkerResultBundle:
        if not envelope.policy_receipt_id:
            raise PolicyDeniedError("POLICY_DENIED")
        if not self.transport or not self.transport.health():
            raise AgentTransportUnavailableError("COMPUTER_USE_TRANSPORT_UNAVAILABLE")
        return super().dispatch(envelope, timeout)

class AstraAdapter(BaseAgentAdapter): pass
