from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List
from ai_engineering.contracts import EffectClass
from ai_engineering.supervisor.router.adapters import AgentAdapter

@dataclass(frozen=True)
class AgentDefinition:
    agent_id: str
    capabilities: List[str]
    supported_message_types: List[str]
    allowed_effect_classes: List[EffectClass]
    timeout_seconds: int
    max_concurrency: int

class AgentRegistry:
    def __init__(self):
        self.adapters: Dict[str, AgentAdapter] = {}
        self.definitions: Dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition, adapter: AgentAdapter):
        self.definitions[definition.agent_id] = definition
        self.adapters[definition.agent_id] = adapter

    def get_adapter(self, agent_id: str) -> AgentAdapter:
        return self.adapters.get(agent_id)
        
    def get_definition(self, agent_id: str) -> AgentDefinition:
        return self.definitions.get(agent_id)
