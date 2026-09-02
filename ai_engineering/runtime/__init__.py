"""Controlled Agent Runtime (PR-13).

Read-only with respect to the control plane: the runtime activates
real, bounded local/WSL agent process execution inside candidate
workspaces authorized by the existing control plane and emits
immutable evidence. It never mutates control-plane state, canonical
repositories, production surfaces, or external systems.
"""

from ai_engineering.runtime.runtime_contracts import (
    RUNTIME_CONTRACT_VERSION,
    RUNTIME_SCHEMA_VERSION,
    AgentExecutionEvidence,
    AgentExecutionRequest,
    AgentProcessIdentity,
    AgentRuntimeError,
    RuntimeBlockingReason,
    RuntimeMode,
)
from ai_engineering.runtime.runtime_policy import (
    RuntimePolicy,
    build_child_environment,
    name_is_secret_like,
    validate_runtime_command,
)
from ai_engineering.runtime.spawn_gate import SpawnAuthorization, authorize_spawn
from ai_engineering.runtime.runtime_registry import (
    RuntimeRegistry,
    RuntimeSlotAllocator,
    SpawnRecord,
)
from ai_engineering.runtime.process_runner import AgentProcessRunner
from ai_engineering.runtime.agent_runtime import ControlledAgentRuntime, ExecutionArtifacts
from ai_engineering.runtime.runtime_evidence import (
    build_candidate_result_from_evidence,
    evidence_run_event_payload,
)

__all__ = [
    "RUNTIME_CONTRACT_VERSION",
    "RUNTIME_SCHEMA_VERSION",
    "AgentExecutionEvidence",
    "AgentExecutionRequest",
    "AgentProcessIdentity",
    "AgentProcessRunner",
    "AgentRuntimeError",
    "ControlledAgentRuntime",
    "ExecutionArtifacts",
    "RuntimeBlockingReason",
    "RuntimeMode",
    "RuntimePolicy",
    "RuntimeRegistry",
    "RuntimeSlotAllocator",
    "SpawnAuthorization",
    "SpawnRecord",
    "authorize_spawn",
    "build_candidate_result_from_evidence",
    "build_child_environment",
    "evidence_run_event_payload",
    "name_is_secret_like",
    "validate_runtime_command",
]
