from __future__ import annotations
from typing import Protocol, Mapping, Any, Optional
from dataclasses import dataclass
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope

class AgentTransportUnavailableError(Exception):
    pass

class AgentTransport(Protocol):
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: ...
    def cancel(self, correlation_id: str) -> None: ...
    def is_available(self) -> bool: ...

class HttpAgentTransport:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._available = True
    
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        if not self.is_available():
            raise AgentTransportUnavailableError("AGENT_TRANSPORT_UNAVAILABLE")
        return WorkerResultBundle(
            schema_version="hermes.worker-result.v1",
            result_id=f"res-{envelope.correlation_id}",
            task_id="task-1",
            attempt_id="att-1",
            worker_id=envelope.target_agent,
            base_sha="sha",
            head_sha="sha",
            canonical_remote="remote",
            repository="repo",
            intent_digest="digest",
            produced_at_utc="utc",
            artifacts=(),
            gate_claims=()
        )
        
    def cancel(self, correlation_id: str) -> None:
        pass
        
    def is_available(self) -> bool:
        return self._available

class AgentAdapter(Protocol):
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: ...
    def cancel(self, correlation_id: str) -> None: ...
    def health(self) -> bool: ...

class BaseAgentAdapter:
    def __init__(self, transport: Optional[AgentTransport] = None):
        self.transport = transport
        
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        if not self.transport or not self.transport.is_available():
            raise AgentTransportUnavailableError(f"{envelope.target_agent.upper()}_TRANSPORT_UNAVAILABLE")
        return self.transport.dispatch(envelope)
        
    def cancel(self, correlation_id: str) -> None:
        if self.transport:
            self.transport.cancel(correlation_id)
            
    def health(self) -> bool:
        return self.transport is not None and self.transport.is_available()

class AntigravityAdapter(BaseAgentAdapter): pass
class CodexAdapter(BaseAgentAdapter): pass
class ComputerUseAdapter(BaseAgentAdapter): pass
class AstraAdapter(BaseAgentAdapter): pass

def mock_bundle(worker_id: str) -> WorkerResultBundle:
    return WorkerResultBundle(
        schema_version="hermes.worker-result.v1",
        result_id="res-1",
        task_id="task-1",
        attempt_id="att-1",
        worker_id=worker_id,
        base_sha="sha",
        head_sha="sha",
        canonical_remote="remote",
        repository="repo",
        intent_digest="digest",
        produced_at_utc="utc",
        artifacts=(),
        gate_claims=()
    )
