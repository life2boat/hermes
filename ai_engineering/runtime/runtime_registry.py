"""Thread-safe runtime registry: idempotent spawns, bounded slots, fenced process events (PR-13).

The registry is runtime-local bookkeeping only. It never mutates
control-plane state; it tracks process identities, idempotent spawn
bookkeeping keyed by stable execution identity, an atomic concurrency
slot allocator bound to the canonical parallelization budget, and
strictly fenced process events/results.

Fencing rule: a process event or result is accepted only when every
binding field (run_id, workspace_id, candidate_id, execution_host_id,
execution_epoch, process_id) matches the registered process identity.
PID reuse can never admit a stale event because ``pid`` is not part of
the durable identity.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ai_engineering.execution.run_contracts import SpawnStatus
from ai_engineering.runtime.runtime_contracts import (
    AgentExecutionRequest,
    AgentExecutionEvidence,
    AgentProcessIdentity,
    AgentRuntimeError,
    RuntimeBlockingReason,
)


@dataclass(frozen=True, slots=True)
class SpawnRecord:
    """Bookkeeping record for one idempotent spawn identity."""

    request: AgentExecutionRequest
    process_identity: AgentProcessIdentity | None
    evidence: AgentExecutionEvidence | None


class RuntimeSlotAllocator:
    """Atomic concurrency slot allocator for candidate processes.

    The budget is supplied from the canonical parallelization policy;
    the allocator never grows it. Check-and-reserve is atomic under a
    lock, so concurrent spawns cannot oversubscribe the budget.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}

    def active_count(self, slot_key: str) -> int:
        with self._lock:
            return sum(1 for key in self._active if key.startswith(slot_key + "#"))

    def reserve(self, slot_key: str, run_id: str, budget: int) -> None:
        """Reserve one slot for ``run_id`` or fail with the canonical budget blocker."""
        with self._lock:
            used = sum(1 for key in self._active if key.startswith(slot_key + "#"))
            if used >= budget:
                raise AgentRuntimeError(
                    "PARALLELIZATION_BUDGET_EXCEEDED",
                    f"Concurrency budget {budget} exceeded for slot {slot_key!r} "
                    f"({used} active processes)",
                )
            self._active[f"{slot_key}#{run_id}"] = run_id

    def release(self, slot_key: str, run_id: str) -> None:
        with self._lock:
            self._active.pop(f"{slot_key}#{run_id}", None)


class RuntimeRegistry:
    """In-memory runtime bookkeeping with idempotent spawn and event fencing."""

    def __init__(self, *, slot_allocator: RuntimeSlotAllocator | None = None) -> None:
        self._lock = threading.Lock()
        self._spawn_records: dict[str, SpawnRecord] = {}
        self._processes: dict[str, AgentProcessIdentity] = {}
        self._results: dict[str, AgentExecutionEvidence] = {}
        self.slot_allocator = slot_allocator or RuntimeSlotAllocator()

    def register_spawn(
        self,
        request: AgentExecutionRequest,
        process_identity: AgentProcessIdentity | None = None,
    ) -> tuple[SpawnStatus, SpawnRecord]:
        """Idempotently register a spawn identity.

        - Same execution_id + equal request: idempotent, returns the
          existing record (ALREADY_ACTIVE).
        - Same execution_id + divergent request: RUNTIME_SPAWN_COLLISION.
        """
        with self._lock:
            existing = self._spawn_records.get(request.execution_id)
            if existing is not None:
                if existing.request != request:
                    raise AgentRuntimeError(
                        RuntimeBlockingReason.RUNTIME_SPAWN_COLLISION.value,
                        f"Spawn identity collision on execution_id {request.execution_id!r}: "
                        "same run identity with divergent request",
                    )
                return SpawnStatus.ALREADY_ACTIVE, existing
            record = SpawnRecord(request=request, process_identity=process_identity, evidence=None)
            self._spawn_records[request.execution_id] = record
            return SpawnStatus.SPAWNED, record

    def get_spawn_record(self, execution_id: str) -> SpawnRecord | None:
        with self._lock:
            return self._spawn_records.get(execution_id)

    def register_process_identity(self, identity: AgentProcessIdentity) -> None:
        with self._lock:
            self._processes[identity.process_id] = identity

    def get_process_identity(self, process_id: str) -> AgentProcessIdentity | None:
        with self._lock:
            return self._processes.get(process_id)

    def record_result(
        self,
        evidence: AgentExecutionEvidence,
        *,
        process_id: str,
        request: AgentExecutionRequest,
    ) -> None:
        """Record execution evidence under strict process fencing.

        Every binding field must match the registered process identity
        and the originating request. Any mismatch is a stale runtime
        event and is rejected fail-closed.
        """
        identity = self._processes.get(process_id)
        if identity is None:
            raise AgentRuntimeError(
                RuntimeBlockingReason.STALE_RUNTIME_EVENT.value,
                f"Result references unregistered process_id {process_id!r}",
            )
        if identity.run_id != request.run_id:
            raise AgentRuntimeError(
                RuntimeBlockingReason.STALE_RUNTIME_EVENT.value,
                "process/run binding mismatch",
            )
        if (
            identity.run_id != evidence.run_id
            or identity.workspace_id != evidence.workspace_id
            or identity.candidate_id != evidence.candidate_id
            or identity.execution_host_id != evidence.execution_host_id
            or identity.execution_epoch != evidence.execution_epoch
            or identity.execution_epoch != request.execution_epoch
        ):
            raise AgentRuntimeError(
                RuntimeBlockingReason.STALE_RUNTIME_EVENT.value,
                "Process identity mismatch (stale runtime event)",
            )
        if (
            evidence.run_id != request.run_id
            or evidence.workspace_id != request.workspace_id
            or evidence.candidate_id != request.candidate_id
            or evidence.execution_host_id != request.execution_host_id
            or evidence.execution_epoch != request.execution_epoch
        ):
            raise AgentRuntimeError(
                RuntimeBlockingReason.STALE_RUNTIME_EVENT.value,
                "Evidence identity mismatch against spawn request",
            )
        with self._lock:
            self._results[evidence.execution_id] = evidence
            existing = self._spawn_records.get(evidence.execution_id)
            if existing is not None:
                self._spawn_records[evidence.execution_id] = SpawnRecord(
                    request=existing.request,
                    process_identity=existing.process_identity,
                    evidence=evidence,
                )

    def get_result(self, execution_id: str) -> AgentExecutionEvidence | None:
        with self._lock:
            return self._results.get(execution_id)

    def list_results(self) -> tuple[AgentExecutionEvidence, ...]:
        with self._lock:
            return tuple(
                self._results[key] for key in sorted(self._results)
            )
