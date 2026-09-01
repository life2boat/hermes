"""Active Run Registry, Idempotent Spawn Controller, and Stale Event Fencer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ai_engineering.execution.run_contracts import (
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
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.workspaces.workspace_contracts import LeaseState

if TYPE_CHECKING:
    from ai_engineering.workspaces.workspace_manager import WorkspaceManager


class ActiveRunRegistry:
    """In-memory domain authority for active execution slots, run identity uniqueness, and stale event fencing."""

    def __init__(self, *, workspace_manager: WorkspaceManager | None = None) -> None:
        self._runs_by_id: dict[str, AgentRunRecord] = {}
        self._active_run_by_slot: dict[str, str] = {}  # slot_key -> active run_id
        self._workspace_manager = workspace_manager

    @property
    def workspace_manager(self) -> WorkspaceManager | None:
        """Return associated WorkspaceManager if configured."""
        return self._workspace_manager

    @staticmethod
    def slot_key(task_id: str, node_id: str, workspace_id: str) -> str:
        """Derive the deterministic slot key for an execution unit."""
        return f"{task_id}:{node_id}:{workspace_id}"

    def get_run(self, run_id: str) -> AgentRunRecord | None:
        """Retrieve an AgentRunRecord by run_id."""
        return self._runs_by_id.get(run_id)

    def list_runs(self) -> list[AgentRunRecord]:
        """List all registered agent run records."""
        return list(self._runs_by_id.values())

    def get_active_run_for_slot(self, task_id: str, node_id: str, workspace_id: str) -> AgentRunRecord | None:
        """Retrieve the currently active AgentRunRecord for an execution slot, if any."""
        s_key = self.slot_key(task_id, node_id, workspace_id)
        active_id = self._active_run_by_slot.get(s_key)
        if active_id is not None:
            return self._runs_by_id.get(active_id)
        return None

    def register_run(
        self,
        identity: AgentRunIdentity,
        *,
        initial_state: RunState = RunState.CREATED,
        now: datetime | None = None,
    ) -> AgentRunRecord:
        """Register an AgentRunIdentity with uniqueness, collision checks, and workspace binding."""
        # Check run_id uniqueness / collision
        if identity.run_id in self._runs_by_id:
            existing = self._runs_by_id[identity.run_id]
            if existing.identity == identity:
                # Idempotent replay of identical run registration
                return existing
            raise RunIdentityError(
                RunBlockingReason.RUN_IDENTITY_COLLISION.value,
                f"run_id {identity.run_id!r} already registered with different immutable identity",
            )

        # Workspace binding validation if WorkspaceManager is provided
        if self._workspace_manager is not None:
            ws = self._workspace_manager.get_workspace(identity.workspace_id)
            if ws is None:
                raise RunIdentityError(
                    RunBlockingReason.UNKNOWN_WORKSPACE.value,
                    f"Workspace {identity.workspace_id!r} not registered in WorkspaceManager",
                )
            if ws.task_id != identity.task_id:
                raise RunIdentityError(
                    RunBlockingReason.RUN_WORKSPACE_MISMATCH.value,
                    f"Workspace task_id {ws.task_id!r} != run task_id {identity.task_id!r}",
                )
            if identity.candidate_id is not None and ws.candidate_id is not None and ws.candidate_id != identity.candidate_id:
                raise RunIdentityError(
                    RunBlockingReason.RUN_WORKSPACE_MISMATCH.value,
                    f"Workspace candidate_id {ws.candidate_id!r} != run candidate_id {identity.candidate_id!r}",
                )

            # Lease ownership verification
            lease = self._workspace_manager.get_lease(identity.workspace_id)
            if lease is not None and lease.state in (LeaseState.ACTIVE, LeaseState.RESERVED):
                if lease.owner_run_id != identity.run_id:
                    raise RunIdentityError(
                        RunBlockingReason.RUN_LEASE_OWNERSHIP_MISMATCH.value,
                        f"Workspace lease owned by {lease.owner_run_id!r} != run_id {identity.run_id!r}",
                    )

        update_time = now if now is not None else datetime.now(timezone.utc)
        record = AgentRunRecord(
            identity=identity,
            state=initial_state,
            updated_at=update_time,
        )
        self._runs_by_id[identity.run_id] = record
        return record

    def spawn_agent(
        self,
        identity: AgentRunIdentity,
        *,
        now: datetime | None = None,
    ) -> tuple[AgentRunRecord, SpawnStatus]:
        """Idempotently initiate a logical execution run, fencing older epochs and duplicate active slots."""
        s_key = self.slot_key(identity.task_id, identity.node_id, identity.workspace_id)

        # Check existing active run in slot
        if s_key in self._active_run_by_slot:
            active_run_id = self._active_run_by_slot[s_key]
            active_record = self._runs_by_id.get(active_run_id)

            if active_record is not None and active_record.is_active():
                if active_run_id == identity.run_id:
                    if active_record.identity != identity:
                        raise RunIdentityError(
                            RunBlockingReason.RUN_IDENTITY_COLLISION.value,
                            f"Spawn request for {identity.run_id!r} conflicts with existing identity",
                        )
                    # Idempotent return of already active run
                    return active_record, SpawnStatus.ALREADY_ACTIVE

                # Different run_id attempting to use the same slot
                if identity.execution_epoch <= active_record.identity.execution_epoch:
                    raise RunIdentityError(
                        RunBlockingReason.DUPLICATE_ACTIVE_RUN.value,
                        f"Slot {s_key} already occupied by active run {active_run_id!r} at epoch {active_record.identity.execution_epoch} >= requested epoch {identity.execution_epoch}",
                    )

        # Register run if not already registered
        record = self.register_run(identity, initial_state=RunState.START_REQUESTED, now=now)

        # Advance to LIVE
        live_record = record.transition(RunState.LIVE, now=now)
        self._runs_by_id[identity.run_id] = live_record
        self._active_run_by_slot[s_key] = identity.run_id

        return live_record, SpawnStatus.SPAWNED

    def request_cancel(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> AgentRunRecord:
        """Request cancellation of an active run without assuming immediate process exit."""
        record = self._runs_by_id.get(run_id)
        if record is None:
            raise RunStateError(
                RunBlockingReason.RUN_NOT_ACTIVE.value,
                f"Run {run_id!r} not found",
            )
        if not record.is_active() or record.state != RunState.LIVE:
            raise RunStateError(
                RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value,
                f"Cannot cancel run {run_id!r} in state {record.state.value}",
            )

        cancelled_record = record.transition(
            RunState.CANCEL_REQUESTED,
            now=now,
            cancellation_reason=reason,
        )
        self._runs_by_id[run_id] = cancelled_record
        return cancelled_record

    def process_event(
        self,
        event: RunEventEnvelope,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str | None, AgentRunRecord | None]:
        """Process an inbound run lifecycle event with strict run_id and epoch fencing."""
        record = self._runs_by_id.get(event.run_id)
        if record is None:
            raise StaleEventError(
                RunBlockingReason.STALE_RUN_EVENT.value,
                f"Event references unregistered run_id {event.run_id!r}",
            )

        s_key = self.slot_key(
            record.identity.task_id,
            record.identity.node_id,
            record.identity.workspace_id,
        )
        active_run_id = self._active_run_by_slot.get(s_key)

        # Stale Run Fencing
        if active_run_id != event.run_id:
            raise StaleEventError(
                RunBlockingReason.STALE_RUN_EVENT.value,
                f"Event run_id {event.run_id!r} is stale for slot {s_key} (current active run is {active_run_id!r})",
            )

        # Stale Epoch Fencing
        active_epoch = record.identity.execution_epoch
        if event.execution_epoch != active_epoch:
            raise StaleEventError(
                RunBlockingReason.STALE_RUN_MUTATION.value,
                f"Event epoch {event.execution_epoch} != active run epoch {active_epoch}",
            )

        # Apply state transitions for lifecycle completion events
        if event.event_type == RunEventType.AGENT_RUN_EXITED:
            raw_code = event.payload.get("exit_code", 0)
            exit_code = int(raw_code) if raw_code is not None else 0
            exited_record = record.transition(RunState.EXITED, now=now, exit_code=exit_code)
            self._runs_by_id[event.run_id] = exited_record
            if self._active_run_by_slot.get(s_key) == event.run_id:
                del self._active_run_by_slot[s_key]
            return True, None, exited_record

        if event.event_type == RunEventType.AGENT_RUN_FAILED:
            err_msg = str(event.payload.get("error_message") or "Agent run failed")
            failed_record = record.transition(RunState.FAILED, now=now, error_message=err_msg)
            self._runs_by_id[event.run_id] = failed_record
            if self._active_run_by_slot.get(s_key) == event.run_id:
                del self._active_run_by_slot[s_key]
            return True, None, failed_record

        return True, None, record
