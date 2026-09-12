from __future__ import annotations
from typing import Protocol, Mapping, Any, Optional
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope

class AgentTransportUnavailableError(Exception):
    pass

class PolicyDeniedError(Exception):
    pass

class ComputerUseTransportUnavailableError(AgentTransportUnavailableError):
    pass

class AgentTransport(Protocol):
    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle: ...
    def cancel(self, operation_id: str) -> Any: ...
    def health(self) -> bool: ...

class AgentAdapter(Protocol):
    def dispatch(self, envelope: AgentEnvelope, timeout: int, provenance: Mapping[str, Any], operation_id: str) -> WorkerResultBundle: ...
    def cancel(self, operation_id: str) -> Any: ...
    def health(self) -> bool: ...

class BaseAgentAdapter:
    def __init__(self, transport: Optional[AgentTransport] = None) -> None:
        self.transport = transport

    def dispatch(self, envelope: AgentEnvelope, timeout: int, provenance: Mapping[str, Any], operation_id: str) -> WorkerResultBundle:
        if not self.transport or not self.transport.health():
            raise AgentTransportUnavailableError("AGENT_TRANSPORT_UNAVAILABLE")

        if not provenance or not provenance.get("repository") or not provenance.get("canonical_remote") or not provenance.get("base_sha"):
            raise ValueError("SOURCE_PROVENANCE_UNRESOLVED")

        request = {
            "message_id": envelope.message_id,
            "worker_id": envelope.recipient_agent,
            "correlation_id": envelope.correlation_id,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "attempt_id": envelope.attempt_id,
            "operation_id": operation_id,
            "repository": provenance["repository"],
            "canonical_remote": provenance["canonical_remote"],
            "base_sha": provenance["base_sha"],
            "objective": envelope.payload.get("objective", ""),
            "allowed_scope": envelope.payload.get("allowed_scope", []),
            "task_intent_id": envelope.task_intent_id,
            "task_intent_digest": envelope.task_intent_digest,
            "policy_receipt_id": envelope.policy_receipt_id,
            "policy_receipt_digest": envelope.policy_receipt_digest,
            "effective_effect_class": envelope.effect_class.value if hasattr(envelope.effect_class, "value") else str(envelope.effect_class),
            "effective_stop_boundary": envelope.stop_boundary.value if hasattr(envelope.stop_boundary, "value") else str(envelope.stop_boundary),
            "required_validators": envelope.payload.get("required_validators", []),
            "result_schema": "hermes.worker-result.v1",
        }
        return self.transport.dispatch(request, timeout)

    def cancel(self, operation_id: str) -> bool:
        if not self.transport:
            return True
        self.transport.cancel(operation_id)
        if hasattr(self.transport, "is_running"):
            return not self.transport.is_running(operation_id)
        return False

    def is_running(self, operation_id: str) -> bool:
        if self.transport and hasattr(self.transport, "is_running"):
            return bool(self.transport.is_running(operation_id))
        return False

    def health(self) -> bool:
        return self.transport is not None and self.transport.health()

class AntigravityAdapter(BaseAgentAdapter):
    pass

class CodexAdapter(BaseAgentAdapter):
    pass

class ComputerUseAdapter(BaseAgentAdapter):
    def dispatch(self, envelope: AgentEnvelope, timeout: int, provenance: Mapping[str, Any], operation_id: str) -> WorkerResultBundle:
        if not envelope.policy_receipt_id or not envelope.policy_receipt_digest:
            raise PolicyDeniedError("POLICY_DENIED")
        if not self.transport or not self.transport.health():
            raise ComputerUseTransportUnavailableError("COMPUTER_USE_TRANSPORT_UNAVAILABLE")
        return super().dispatch(envelope, timeout, provenance, operation_id)

class AstraAdapter(BaseAgentAdapter):
    pass
