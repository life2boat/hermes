from __future__ import annotations
from typing import Protocol, Mapping, Any
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope

class AgentAdapter(Protocol):
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: ...
    def cancel(self, correlation_id: str) -> None: ...
    def health(self) -> bool: ...

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

class AntigravityAdapter:
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: return mock_bundle("antigravity")
    def cancel(self, correlation_id: str) -> None: pass
    def health(self) -> bool: return True

class CodexAdapter:
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: return mock_bundle("codex")
    def cancel(self, correlation_id: str) -> None: pass
    def health(self) -> bool: return True

class ComputerUseAdapter:
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: return mock_bundle("computer_use")
    def cancel(self, correlation_id: str) -> None: pass
    def health(self) -> bool: return True
    
class AstraAdapter:
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle: return mock_bundle("astra")
    def cancel(self, correlation_id: str) -> None: pass
    def health(self) -> bool: return True
