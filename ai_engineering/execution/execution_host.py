"""Abstract base class interface for ExecutionHost."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ai_engineering.execution.host_contracts import (
    ExecutionHostIdentity,
    ExecutionRequest,
    ExecutionResult,
    HostStatus,
)
from ai_engineering.execution.run_contracts import AgentRunIdentity
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity


class ExecutionHost(ABC):
    """Abstract interface defining the execution boundary for agent commands."""

    @abstractmethod
    def identity(self) -> ExecutionHostIdentity:
        """Return the immutable identity and advertised capabilities of this host."""

    @abstractmethod
    def probe(self) -> HostStatus:
        """Probe the execution environment health and availability."""

    @abstractmethod
    def validate_request(
        self,
        request: ExecutionRequest,
        workspace_identity: WorkspaceIdentity | None = None,
        run_identity: AgentRunIdentity | None = None,
    ) -> tuple[bool, tuple[str, ...]]:
        """Validate request consistency, workspace/run/host binding, and path containment."""

    @abstractmethod
    def execute(
        self,
        request: ExecutionRequest,
        workspace_identity: WorkspaceIdentity | None = None,
        run_identity: AgentRunIdentity | None = None,
    ) -> ExecutionResult:
        """Execute a command request on this host with timeout and output bounds."""

    @abstractmethod
    def request_cancel(self, execution_id: str) -> bool:
        """Request cancellation of an active execution."""
