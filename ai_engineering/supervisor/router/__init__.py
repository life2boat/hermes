"""Cross-Agent Messaging Router."""

from ai_engineering.supervisor.router.envelope import AgentEnvelope, payload_digest
from ai_engineering.supervisor.router.adapters import AgentAdapter, AntigravityAdapter, CodexAdapter, ComputerUseAdapter
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.supervisor.router.router import CrossAgentRouter, PersistentStore, FilePersistentStore

__all__ = [
    "AgentEnvelope",
    "payload_digest",
    "AgentAdapter",
    "AntigravityAdapter",
    "CodexAdapter",
    "ComputerUseAdapter",
    "AgentRegistry",
    "CrossAgentRouter",
    "PersistentStore",
    "FilePersistentStore",
]
