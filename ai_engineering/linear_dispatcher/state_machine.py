"""Deterministic state machine for autonomous task dispatch lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ai_engineering.linear_dispatcher.contracts import (
    StateTransitionRecord,
    TaskState,
)


class InvalidStateTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""


_VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DISCOVERED: frozenset([
        TaskState.CLAIMED,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.CLAIMED: frozenset([
        TaskState.WORKTREE_READY,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.WORKTREE_READY: frozenset([
        TaskState.IMPLEMENTING,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.IMPLEMENTING: frozenset([
        TaskState.VALIDATING,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.VALIDATING: frozenset([
        TaskState.COMMITTED,
        TaskState.PR_OPEN,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.COMMITTED: frozenset([
        TaskState.BRANCH_PUSHED,
        TaskState.PR_OPEN,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.BRANCH_PUSHED: frozenset([
        TaskState.PR_OPEN,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.PR_OPEN: frozenset([
        TaskState.CI_PENDING,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.CI_PENDING: frozenset([
        TaskState.CI_PASS,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.CI_PASS: frozenset([
        TaskState.WRITEBACK_DONE,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.WRITEBACK_DONE: frozenset([
        TaskState.DONE,
        TaskState.BLOCKED,
        TaskState.FAILED,
    ]),
    TaskState.DONE: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.BLOCKED: frozenset(),
}


class TaskStateMachine:
    """Manages sequential, evidence-backed state transitions for a task."""

    def __init__(self, task_id: str, initial_state: TaskState = TaskState.DISCOVERED) -> None:
        self._task_id = task_id
        self._current_state = initial_state
        self._history: list[StateTransitionRecord] = []
        # Record initial discovery
        self._history.append(
            StateTransitionRecord(
                task_id=task_id,
                from_state=initial_state,
                to_state=initial_state,
                transitioned_at=datetime.now(timezone.utc).isoformat(),
                evidence={"event": "INIT"},
            )
        )

    @property
    def current_state(self) -> TaskState:
        return self._current_state

    @property
    def history(self) -> tuple[StateTransitionRecord, ...]:
        return tuple(self._history)

    def transition(
        self,
        to_state: TaskState,
        evidence: dict[str, Any] | None = None,
        reason: str | None = None,
        now_iso: str | None = None,
    ) -> StateTransitionRecord:
        """Attempt to transition to next state.

        Raises InvalidStateTransitionError if the transition violates the contract.
        """
        allowed = _VALID_TRANSITIONS.get(self._current_state, frozenset())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot transition task {self._task_id} from {self._current_state} to {to_state}. "
                f"Allowed transitions: {sorted([s.value for s in allowed])}"
            )

        if now_iso is None:
            now_iso = datetime.now(timezone.utc).isoformat()

        rec = StateTransitionRecord(
            task_id=self._task_id,
            from_state=self._current_state,
            to_state=to_state,
            transitioned_at=now_iso,
            evidence=evidence or {},
            reason=reason,
        )
        self._history.append(rec)
        self._current_state = to_state
        return rec
