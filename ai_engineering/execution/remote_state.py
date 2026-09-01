"""Deterministic remote execution state machine and lifecycle manager."""

from __future__ import annotations

import threading
from typing import Mapping

from ai_engineering.execution.remote_contracts import (
    ReconciliationOutcome,
    RemoteBlockingReason,
    RemoteEventEnvelope,
    RemoteEventType,
    RemoteExecutionError,
    RemoteExecutionState,
    RemoteProcessIdentity,
    RemoteReconciliationResult,
    RemoteSessionIdentity,
)


class RemoteExecutionLifecycle:
    """State machine tracking and verifying remote execution state transitions."""

    def __init__(
        self,
        process_identity: RemoteProcessIdentity,
        initial_state: RemoteExecutionState = RemoteExecutionState.CREATED,
    ) -> None:
        self.process_identity = process_identity
        self._state = initial_state
        self._exit_code: int | None = None
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._blockers: list[str] = []

    @property
    def state(self) -> RemoteExecutionState:
        with self._lock:
            return self._state

    @property
    def exit_code(self) -> int | None:
        with self._lock:
            return self._exit_code

    @property
    def blockers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._blockers)

    def transition_to(
        self,
        new_state: RemoteExecutionState,
        session_id: str,
        execution_epoch: int,
        exit_code: int | None = None,
        blocker: str | None = None,
    ) -> None:
        """Apply a validated state transition with epoch and session fencing."""
        with self._lock:
            # Stale session check
            if session_id != self.process_identity.session_id:
                raise RemoteExecutionError(
                    RemoteBlockingReason.STALE_RUN_EVENT.value,
                    f"Event session_id {session_id!r} != active session_id {self.process_identity.session_id!r}",
                )

            # Stale epoch check
            if execution_epoch != self.process_identity.execution_epoch:
                raise RemoteExecutionError(
                    RemoteBlockingReason.STALE_RUN_EVENT.value,
                    f"Event execution_epoch {execution_epoch} != active epoch {self.process_identity.execution_epoch}",
                )

            # Invariant: once in terminal EXITED or FAILED, cannot regress
            if self._state in (RemoteExecutionState.EXITED, RemoteExecutionState.FAILED):
                raise RemoteExecutionError(
                    RemoteBlockingReason.STALE_RUN_MUTATION.value,
                    f"Cannot transition from terminal state {self._state} to {new_state}",
                )

            # Invariant: Disconnect / Unverifiable must never fabricate EXITED
            if new_state == RemoteExecutionState.EXITED and exit_code is None:
                raise RemoteExecutionError(
                    RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                    "Terminal EXITED state requires a confirmed integer exit code",
                )

            if new_state == RemoteExecutionState.DISCONNECTED:
                self._blockers.append(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value)
            elif new_state == RemoteExecutionState.UNVERIFIABLE:
                self._blockers.append(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value)

            if blocker:
                self._blockers.append(blocker)

            self._state = new_state
            if exit_code is not None:
                self._exit_code = exit_code

    def apply_reconciliation(self, result: RemoteReconciliationResult) -> None:
        """Apply deterministic reconciliation outcome."""
        with self._lock:
            if result.session_id != self.process_identity.session_id or result.execution_epoch != self.process_identity.execution_epoch:
                raise RemoteExecutionError(
                    RemoteBlockingReason.STALE_RUN_EVENT.value,
                    "Reconciliation result identity does not match active process",
                )

            if result.outcome == ReconciliationOutcome.CONFIRMED_LIVE:
                self._state = RemoteExecutionState.LIVE
                self._blockers = [b for b in self._blockers if b != RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value]
            elif result.outcome == ReconciliationOutcome.CONFIRMED_EXITED:
                self._state = RemoteExecutionState.EXITED
                self._exit_code = result.exit_code if result.exit_code is not None else 0
                self._blockers = [b for b in self._blockers if b != RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value]
            elif result.outcome == ReconciliationOutcome.UNVERIFIABLE:
                self._state = RemoteExecutionState.UNVERIFIABLE
                if RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value not in self._blockers:
                    self._blockers.append(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value)
            else:
                self._state = RemoteExecutionState.FAILED
                if RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value not in self._blockers:
                    self._blockers.append(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value)
