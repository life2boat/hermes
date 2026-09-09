"""Dispatch package for autonomous supervisor (Task No.4)."""

from ai_engineering.supervisor.dispatch.contracts import (
    DISPATCH_ENVELOPE_SCHEMA_VERSION,
    DISPATCH_RECEIPT_SCHEMA_VERSION,
    SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION,
    DispatchEnvelope,
    DispatchReceipt,
    DispatchStatus,
    SourceTransitionReceipt,
    compute_deterministic_digest,
    compute_dispatch_id,
)
from ai_engineering.supervisor.dispatch.coordinator import DispatchCoordinator
from ai_engineering.supervisor.dispatch.github import (
    CIDetailedState,
    CIState,
    FakeGitHubProvider,
    GitHubProvider,
    PRState,
)
from ai_engineering.supervisor.dispatch.worker import (
    AntigravityAdapter,
    FakeWorkerDispatcher,
    WorkerDispatcher,
)

__all__ = [
    "AntigravityAdapter",
    "CIDetailedState",
    "CIState",
    "DISPATCH_ENVELOPE_SCHEMA_VERSION",
    "DISPATCH_RECEIPT_SCHEMA_VERSION",
    "DispatchCoordinator",
    "DispatchEnvelope",
    "DispatchReceipt",
    "DispatchStatus",
    "FakeGitHubProvider",
    "FakeWorkerDispatcher",
    "GitHubProvider",
    "PRState",
    "SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION",
    "SourceTransitionReceipt",
    "WorkerDispatcher",
    "compute_deterministic_digest",
    "compute_dispatch_id",
]
