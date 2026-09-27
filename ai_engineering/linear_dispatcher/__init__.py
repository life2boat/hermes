"""Hermes Linear Autonomous Dispatcher package."""

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    ClaimRecord,
    DispatcherConfig,
    LinearTask,
    StateTransitionRecord,
    TaskState,
)
from ai_engineering.linear_dispatcher.dispatcher import (
    AutonomousDispatcher,
    DispatchResult,
)
from ai_engineering.linear_dispatcher.lease_manager import (
    LeaseError,
    LeaseManager,
)
from ai_engineering.linear_dispatcher.scope_gate import ScopeGate
from ai_engineering.linear_dispatcher.state_machine import (
    InvalidStateTransitionError,
    TaskStateMachine,
)
from ai_engineering.linear_dispatcher.task_filter import (
    is_eligible_task,
    select_next_task,
    sort_tasks,
)
from ai_engineering.linear_dispatcher.worktree_service import (
    WorktreeService,
    compute_branch_name,
)
from ai_engineering.linear_dispatcher.linear_client import LinearCodexClient
from ai_engineering.linear_dispatcher.writeback import (
    ExecutionEvidence,
    ILinearClient,
    WritebackService,
)

__all__ = [
    "AutonomousDispatcher",
    "BlockReasonCode",
    "ClaimRecord",
    "DispatcherConfig",
    "DispatchResult",
    "ExecutionEvidence",
    "ILinearClient",
    "InvalidStateTransitionError",
    "LeaseError",
    "LeaseManager",
    "LinearCodexClient",
    "LinearTask",
    "ScopeGate",
    "StateTransitionRecord",
    "TaskState",
    "TaskStateMachine",
    "WorktreeService",
    "WritebackService",
    "compute_branch_name",
    "is_eligible_task",
    "select_next_task",
    "sort_tasks",
]
