"""State machine and record model for Agent Execution Runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
    RunBlockingReason,
    RunState,
    RunStateError,
    _format_iso_datetime,
    _validate_iso_datetime,
)

_VALID_RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.START_REQUESTED, RunState.FAILED}),
    RunState.START_REQUESTED: frozenset({RunState.LIVE, RunState.FAILED}),
    RunState.LIVE: frozenset({RunState.CANCEL_REQUESTED, RunState.EXITED, RunState.FAILED}),
    RunState.CANCEL_REQUESTED: frozenset({RunState.EXITED, RunState.FAILED}),
    RunState.EXITED: frozenset(),   # Terminal state
    RunState.FAILED: frozenset(),   # Terminal state
}


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    """Immutable domain snapshot of an agent run's lifecycle state and metadata."""

    identity: AgentRunIdentity
    state: RunState
    updated_at: datetime
    cancellation_reason: str | None = None
    exit_code: int | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AgentRunIdentity):
            raise RunStateError(RunBlockingReason.INVALID_RUN_IDENTITY.value, "identity must be AgentRunIdentity")
        if not isinstance(self.state, RunState):
            raise RunStateError("INVALID_RUN_STATE", f"state must be RunState, got {self.state!r}")
        if not isinstance(self.updated_at, datetime):
            raise RunStateError("INVALID_UPDATED_AT", "updated_at must be datetime")

    def is_active(self) -> bool:
        """Check if the run is currently in an active or in-flight state."""
        return self.state in (
            RunState.CREATED,
            RunState.START_REQUESTED,
            RunState.LIVE,
            RunState.CANCEL_REQUESTED,
        )

    def is_terminal(self) -> bool:
        """Check if the run has reached a terminal outcome."""
        return self.state in (RunState.EXITED, RunState.FAILED)

    def transition(
        self,
        new_state: RunState,
        *,
        now: datetime | None = None,
        cancellation_reason: str | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
    ) -> AgentRunRecord:
        """Advance run state following the fail-closed state transition table."""
        if not isinstance(new_state, RunState):
            raise RunStateError(
                RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value,
                f"Target state must be RunState enum, got {new_state!r}",
            )
        allowed = _VALID_RUN_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise RunStateError(
                RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value,
                f"Invalid run state transition: {self.state.value} -> {new_state.value}",
            )
        update_time = _validate_iso_datetime(now) if now is not None else datetime.now(timezone.utc)
        return AgentRunRecord(
            identity=self.identity,
            state=new_state,
            updated_at=update_time,
            cancellation_reason=cancellation_reason or self.cancellation_reason,
            exit_code=exit_code if exit_code is not None else self.exit_code,
            error_message=error_message or self.error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize run record to canonical dictionary."""
        return {
            "identity": self.identity.to_dict(),
            "state": self.state.value,
            "updated_at": _format_iso_datetime(self.updated_at),
            "cancellation_reason": self.cancellation_reason,
            "exit_code": self.exit_code,
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        """Serialize to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentRunRecord:
        """Deserialize run record from dictionary."""
        if not isinstance(payload, Mapping):
            raise RunStateError("INVALID_PAYLOAD", "Payload must be a mapping")
        raw_ident = payload.get("identity")
        if not isinstance(raw_ident, Mapping):
            raise RunStateError("INVALID_IDENTITY", "identity must be mapping")
        ident = AgentRunIdentity.from_dict(raw_ident)
        raw_state = payload.get("state")
        try:
            state = RunState(str(raw_state))
        except ValueError as exc:
            raise RunStateError("INVALID_RUN_STATE", f"Unknown state: {raw_state!r}") from exc
        updated_at = _validate_iso_datetime(payload.get("updated_at"))
        return cls(
            identity=ident,
            state=state,
            updated_at=updated_at,
            cancellation_reason=payload.get("cancellation_reason"),
            exit_code=payload.get("exit_code"),
            error_message=payload.get("error_message"),
        )

    @classmethod
    def from_json(cls, raw: str) -> AgentRunRecord:
        """Deserialize run record from JSON string."""
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise RunStateError("INVALID_JSON", f"Could not parse JSON: {exc}") from exc
        return cls.from_dict(payload)
