"""Abstract transport interface and contract-only fail-closed implementation for SSH-ready remote hosts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ai_engineering.execution.host_contracts import ExecutionRequest
from ai_engineering.execution.remote_contracts import (
    ReconciliationOutcome,
    RemoteBlockingReason,
    RemoteExecutionError,
    RemoteHostState,
    RemoteProcessIdentity,
    RemoteReconciliationRequest,
    RemoteReconciliationResult,
    RemoteSessionIdentity,
)


class RemoteExecutionTransport(ABC):
    """Abstract interface defining the remote transport boundary for agent commands."""

    @abstractmethod
    def probe(self) -> RemoteHostState:
        """Probe remote host reachability and health (read-only)."""

    @abstractmethod
    def connect(self, session_identity: RemoteSessionIdentity) -> bool:
        """Establish a remote session (contract-level)."""

    @abstractmethod
    def disconnect(self, session_id: str) -> bool:
        """Terminate a remote transport session."""

    @abstractmethod
    def start_execution(
        self,
        request: ExecutionRequest,
        session: RemoteSessionIdentity,
    ) -> RemoteProcessIdentity:
        """Start a command on the remote host."""

    @abstractmethod
    def request_cancel(self, process_identity: RemoteProcessIdentity) -> bool:
        """Request cancellation of a remote process."""

    @abstractmethod
    def reconcile(self, req: RemoteReconciliationRequest) -> RemoteReconciliationResult:
        """Reconcile remote process state following a disconnection or uncertain status."""


class ContractOnlyRemoteTransport(RemoteExecutionTransport):
    """Fail-closed contract-only implementation for PR-10 (refuses real network/SSH operations)."""

    def __init__(
        self,
        execution_host_id: str = "host-remote-contract",
        simulated_state: RemoteHostState = RemoteHostState.UNAVAILABLE,
    ) -> None:
        self.execution_host_id = execution_host_id
        self._simulated_state = simulated_state

    def probe(self) -> RemoteHostState:
        return self._simulated_state

    def connect(self, session_identity: RemoteSessionIdentity) -> bool:
        raise RemoteExecutionError(
            RemoteBlockingReason.REMOTE_CONNECTION_FAILED.value,
            "Real SSH transport is disabled in PR-10 contract-only mode",
        )

    def disconnect(self, session_id: str) -> bool:
        return True

    def start_execution(
        self,
        request: ExecutionRequest,
        session: RemoteSessionIdentity,
    ) -> RemoteProcessIdentity:
        raise RemoteExecutionError(
            RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value,
            "Remote execution cannot be started in PR-10 contract-only mode",
        )

    def request_cancel(self, process_identity: RemoteProcessIdentity) -> bool:
        return False

    def reconcile(self, req: RemoteReconciliationRequest) -> RemoteReconciliationResult:
        return RemoteReconciliationResult(
            execution_id=req.execution_id,
            run_id=req.run_id,
            execution_host_id=req.execution_host_id,
            session_id=req.session_id,
            execution_epoch=req.execution_epoch,
            outcome=ReconciliationOutcome.UNVERIFIABLE,
            process_confirmed_live=False,
            process_confirmed_exited=False,
            exit_code=None,
            evidence="Reconciliation in PR-10 contract-only mode remains UNVERIFIABLE",
            reconciled_at="2026-09-01T00:00:00Z",
            blockers=(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value,),
        )
