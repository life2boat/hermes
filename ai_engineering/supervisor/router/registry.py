from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Sequence
from ai_engineering.contracts import EffectClass
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.supervisor.router.adapters import AgentAdapter

@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    capabilities: Sequence[str]
    supported_message_types: Sequence[MessageType | str]
    allowed_effect_classes: Sequence[EffectClass]
    timeout_seconds: int
    max_concurrency: int = 1
    health_state: str = "HEALTHY"

class AgentRegistry:
    def __init__(self) -> None:
        self.adapters: Dict[str, AgentAdapter] = {}
        self.definitions: Dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition, adapter: AgentAdapter) -> None:
        self.definitions[definition.agent_id] = definition
        self.adapters[definition.agent_id] = adapter

    def get_adapter(self, agent_id: str) -> AgentAdapter | None:
        return self.adapters.get(agent_id)

    def get_definition(self, agent_id: str) -> AgentDefinition | None:
        return self.definitions.get(agent_id)
