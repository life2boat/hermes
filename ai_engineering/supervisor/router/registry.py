from __future__ import annotations
from dataclasses import dataclass
from ai_engineering.supervisor.router.adapters import AgentAdapter, AntigravityAdapter, CodexAdapter, ComputerUseAdapter, AstraAdapter

@dataclass(frozen=True)
class AgentCapability:
    agent_id: str
    capabilities: tuple[str, ...]
    timeout_seconds: int

class AgentRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        self._capabilities: dict[str, AgentCapability] = {}
        
    def register(self, name: str, adapter: AgentAdapter, cap: AgentCapability = None) -> None:
        if name in self._adapters: raise ValueError(f"Agent {name} already registered")
        self._adapters[name] = adapter
        self._capabilities[name] = cap or AgentCapability(agent_id=name, capabilities=(), timeout_seconds=30)
        
    def get_adapter(self, name: str) -> AgentAdapter:
        if name not in self._adapters: raise KeyError(f"Agent {name} not found")
        return self._adapters[name]
        
    def get_capability(self, name: str) -> AgentCapability:
        return self._capabilities.get(name, AgentCapability(agent_id=name, capabilities=(), timeout_seconds=30))
        
    @classmethod
    def create_default(cls) -> AgentRegistry:
        registry = cls()
        registry.register("antigravity", AntigravityAdapter(), AgentCapability("antigravity", ("code",), 60))
        registry.register("codex", CodexAdapter(), AgentCapability("codex", ("code",), 60))
        registry.register("computer_use", ComputerUseAdapter(), AgentCapability("computer_use", ("gui",), 120))
        registry.register("astra", AstraAdapter(), AgentCapability("astra", ("propose",), 30))
        return registry
