"""Control plane contracts, enums, error types, and reason codes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from ai_engineering.execution.host_contracts import ExecutionHostIdentity, ExecutionMode
from ai_engineering.execution.remote_contracts import RemoteBlockingReason
from ai_engineering.execution.run_contracts import AgentRunIdentity, RunBlockingReason
from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)

CONTROL_PLANE_CONTRACT_VERSION = "4.1.0"


class ControlPlanePhase(StrEnum):
    """Explicit bounded phases of an Engineering Cycle."""

    CREATED = "CREATED"
    QUALIFIED = "QUALIFIED"
    PLANNED = "PLANNED"
    PREPARING = "PREPARING"
    INVESTIGATING = "INVESTIGATING"
    IMPLEMENTING = "IMPLEMENTING"
    JUDGING = "JUDGING"
    VALIDATING = "VALIDATING"
    REQUALIFYING = "REQUALIFYING"
    READY_FOR_HANDOFF = "READY_FOR_HANDOFF"
    BLOCKED = "BLOCKED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class ControlPlaneEventType(StrEnum):
    """Domain event types recognized by the Control Plane."""

    WORKSPACE_READY = "WORKSPACE_READY"
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"
    CANDIDATE_COMPLETED = "CANDIDATE_COMPLETED"
    JUDGEMENT_COMPLETED = "JUDGEMENT_COMPLETED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    REQUALIFICATION_COMPLETED = "REQUALIFICATION_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"
    BLOCKER_RAISED = "BLOCKER_RAISED"


class ControlPlaneBlockingReason(StrEnum):
    """Machine-readable reason codes for control plane blockers."""

    CONTROL_PLANE_STATE_INVALID = "CONTROL_PLANE_STATE_INVALID"
    CONTROL_PLANE_EVENT_COLLISION = "CONTROL_PLANE_EVENT_COLLISION"
    CONTROL_PLANE_STALE_EVENT = "CONTROL_PLANE_STALE_EVENT"
    CONTROL_PLANE_HANDOFF_INCOMPLETE = "CONTROL_PLANE_HANDOFF_INCOMPLETE"
    CONTROL_PLANE_AUTHORIZATION_MISMATCH = "CONTROL_PLANE_AUTHORIZATION_MISMATCH"
    CONTROL_PLANE_BARRIER_NOT_READY = "CONTROL_PLANE_BARRIER_NOT_READY"
    REMOTE_EXECUTION_UNVERIFIABLE = "REMOTE_EXECUTION_UNVERIFIABLE"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    EXECUTION_HOST_MISMATCH = "EXECUTION_HOST_MISMATCH"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"


class ControlPlaneError(Exception):
    """Fail-closed error for control plane invariant and contract violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
