"""Hermes Agent Run Identity, Execution Host Abstraction & Execution Plane package."""

from ai_engineering.execution.execution_host import ExecutionHost
from ai_engineering.execution.execution_registry import ExecutionRegistry
from ai_engineering.execution.host_contracts import (
    DEFAULT_MAX_OUTPUT_BYTES,
    EXECUTION_HOST_CONTRACT_VERSION,
    ExecutionHostError,
    ExecutionHostIdentity,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    HostBlockingReason,
    HostCapability,
    HostPlatform,
    HostStatus,
    WslExecutionConfig,
)
from ai_engineering.execution.local_host import (
    LocalExecutionHost,
    detect_current_platform,
)
from ai_engineering.execution.run_contracts import (
    AGENT_RUN_CONTRACT_VERSION,
    RUN_EVENT_SCHEMA_VERSION,
    AgentRunIdentity,
    RunBlockingReason,
    RunEventEnvelope,
    RunEventType,
    RunIdentityError,
    RunState,
    RunStateError,
    SpawnStatus,
    StaleEventError,
)
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.execution.wsl_host import WslExecutionHost

__all__ = [
    "AGENT_RUN_CONTRACT_VERSION",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "EXECUTION_HOST_CONTRACT_VERSION",
    "ActiveRunRegistry",
    "AgentRunIdentity",
    "AgentRunRecord",
    "ExecutionHost",
    "ExecutionHostError",
    "ExecutionHostIdentity",
    "ExecutionMode",
    "ExecutionRegistry",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionState",
    "HostBlockingReason",
    "HostCapability",
    "HostPlatform",
    "HostStatus",
    "LocalExecutionHost",
    "RUN_EVENT_SCHEMA_VERSION",
    "RunBlockingReason",
    "RunEventEnvelope",
    "RunEventType",
    "RunIdentityError",
    "RunState",
    "RunStateError",
    "SpawnStatus",
    "StaleEventError",
    "WslExecutionConfig",
    "WslExecutionHost",
    "detect_current_platform",
]
