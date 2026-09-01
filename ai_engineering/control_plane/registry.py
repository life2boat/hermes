"""In-memory thread-safe registry for EngineeringCycleState, events, and handoffs (PR-11.1).

Collision semantics (D8): recording an item whose identity already exists
is idempotent only when the recorded value is exactly equal; any
different value under the same identity is a collision failure.
Last-writer-wins overwrites are structurally impossible.
"""

from __future__ import annotations

import threading

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff


class EngineeringCycleRegistry:
    """In-memory registry tracking active engineering cycles, events, and node handoffs.

    Non-durable by design (CP10): state reconstruction after a process
    crash must come from external immutable event logs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cycles: dict[str, EngineeringCycleState] = {}
        self._events: dict[str, list[ControlPlaneEvent]] = {}
        self._handoffs: dict[str, NodeHandoff] = {}

    def register_cycle(self, cycle: EngineeringCycleState) -> None:
        with self._lock:
            if cycle.cycle_id in self._cycles:
                existing = self._cycles[cycle.cycle_id]
                if existing == cycle:
                    return
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cycle collision on cycle_id: {cycle.cycle_id}",
                )
            self._cycles[cycle.cycle_id] = cycle

    def update_cycle(self, cycle: EngineeringCycleState) -> None:
        with self._lock:
            existing = self._cycles.get(cycle.cycle_id)
            if existing is not None and existing != cycle:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cycle identity collision on update for cycle_id: {cycle.cycle_id}",
                )
            self._cycles[cycle.cycle_id] = cycle

    def record_event(self, event: ControlPlaneEvent) -> None:
        with self._lock:
            events = self._events.setdefault(event.cycle_id, [])
            for existing in events:
                if existing.event_id == event.event_id:
                    if existing == event:
                        return  # Idempotent duplicate
                    raise ControlPlaneError(
                        ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value,
                        f"Event collision on event_id: {event.event_id}",
                    )
            events.append(event)

    def record_handoff(self, handoff: NodeHandoff) -> None:
        with self._lock:
            existing = self._handoffs.get(handoff.handoff_id)
            if existing is not None:
                if existing == handoff:
                    return  # Idempotent duplicate
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value,
                    f"Handoff collision on handoff_id: {handoff.handoff_id}",
                )
            self._handoffs[handoff.handoff_id] = handoff

    def get_cycle(self, cycle_id: str) -> EngineeringCycleState | None:
        with self._lock:
            return self._cycles.get(cycle_id)

    def get_events(self, cycle_id: str) -> tuple[ControlPlaneEvent, ...]:
        with self._lock:
            return tuple(self._events.get(cycle_id, []))

    def get_handoff(self, handoff_id: str) -> NodeHandoff | None:
        with self._lock:
            return self._handoffs.get(handoff_id)
