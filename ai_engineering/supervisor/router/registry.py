"""Agent Registry."""

from __future__ import annotations
from ai_engineering.supervisor.router.adapters import AgentAdapter, AntigravityAdapter, CodexAdapter, ComputerUseAdapter

class AgentRegistry:
    """Registry of known agents."""
    
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        
    def register(self, name: str, adapter: AgentAdapter) -> None:
        if name in self._adapters:
            raise ValueError(f"Agent {name} already registered")
        self._adapters[name] = adapter
        
    def get_adapter(self, name: str) -> AgentAdapter:
        if name not in self._adapters:
            raise KeyError(f"Agent {name} not found")
        return self._adapters[name]
        
    @classmethod
    def create_default(cls) -> AgentRegistry:
        registry = cls()
        registry.register("antigravity", AntigravityAdapter())
        registry.register("codex", CodexAdapter())
        registry.register("computer_use", ComputerUseAdapter())
        return registry
