"""Agent Adapters."""

from __future__ import annotations
from typing import Protocol, Mapping, Any
from ai_engineering.supervisor.worker_result import WorkerResultBundle
from ai_engineering.supervisor.router.envelope import AgentEnvelope

class AgentAdapter(Protocol):
    """Interface for routing messages to agents."""
    
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        """Dispatch message to the agent and return the result bundle."""
        ...

class AntigravityAdapter:
    """Adapter for Antigravity agent."""
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        # Stub implementation
        pass

class CodexAdapter:
    """Adapter for Codex agent."""
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        # Stub implementation
        pass

class ComputerUseAdapter:
    """Adapter for Computer Use agent."""
    def dispatch(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        # Stub implementation
        pass
