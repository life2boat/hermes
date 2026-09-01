"""Autonomous Control Plane package for Hermes v4.1 (PR-11.1 hardened)."""

from ai_engineering.control_plane._evidence_refs import validate_evidence_ref
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import (
    CONTROL_PLANE_CONTRACT_VERSION,
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
    ValidationEvidence,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.orchestrator import (
    EngineeringCycleOrchestrator,
    EngineeringCycleResult,
)
from ai_engineering.control_plane.registry import EngineeringCycleRegistry

__all__ = [
    "CONTROL_PLANE_CONTRACT_VERSION",
    "ControlPlaneBlockingReason",
    "ControlPlaneError",
    "ControlPlaneEvent",
    "ControlPlaneEventType",
    "ControlPlanePhase",
    "EngineeringCycleOrchestrator",
    "EngineeringCycleRegistry",
    "EngineeringCycleResult",
    "EngineeringCycleState",
    "NodeHandoff",
    "ProductionSerializationBarrier",
    "ValidationEvidence",
    "validate_evidence_ref",
]
