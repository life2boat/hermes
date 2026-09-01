"""Operator Observability Plane contracts (PR-12).

The observability plane is a deterministic, read-only PROJECTION over
existing authoritative contracts (PR-1..PR-11.1). It owns no lifecycle
state, grants no authority, and can never mutate control-plane objects.

Projection states and operator health states are descriptive only.
They are not execution commands.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


OBSERVABILITY_SCHEMA_VERSION = 1
OBSERVABILITY_CONTRACT_VERSION = "4.2.0"


class ProjectionStatus(StrEnum):
    """Explicit projection state. Never collapse non-error states into HEALTHY."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class OperatorHealthState(StrEnum):
    """Overall operator-facing health derived from evidence.

    Precedence (highest first): CONFLICTED > UNVERIFIABLE > BLOCKED >
    STALE > DEGRADED > OK. Missing safety-critical evidence can never
    render OK (no false green).
    """

    OK = "OK"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class ObservabilityReasonCode(StrEnum):
    """Observability-only machine reason codes.

    These are projection-layer codes. Canonical control-plane blockers
    (e.g. REMOTE_EXECUTION_UNVERIFIABLE, CONTROL_PLANE_*) are always
    surfaced under their canonical names; this enum never duplicates
    them.
    """

    OBSERVABILITY_PROJECTION_INCOMPLETE = "OBSERVABILITY_PROJECTION_INCOMPLETE"
    OBSERVABILITY_PROJECTION_STALE = "OBSERVABILITY_PROJECTION_STALE"
    OBSERVABILITY_IDENTITY_CONFLICT = "OBSERVABILITY_IDENTITY_CONFLICT"
    OBSERVABILITY_SOURCE_UNVERIFIABLE = "OBSERVABILITY_SOURCE_UNVERIFIABLE"
    OBSERVABILITY_REDACTION_REQUIRED = "OBSERVABILITY_REDACTION_REQUIRED"
    OBSERVABILITY_OUTPUT_LIMIT_EXCEEDED = "OBSERVABILITY_OUTPUT_LIMIT_EXCEEDED"
    OBSERVABILITY_OUTPUT_UNBOUNDED = "OBSERVABILITY_OUTPUT_UNBOUNDED"
    OBSERVABILITY_SCHEMA_UNSUPPORTED = "OBSERVABILITY_SCHEMA_UNSUPPORTED"
    OBSERVABILITY_EVIDENCE_MISSING = "OBSERVABILITY_EVIDENCE_MISSING"


class BarrierName(StrEnum):
    """Canonical barrier identities exposed with explanations."""

    VALIDATION = "VALIDATION"
    REQUALIFICATION = "REQUALIFICATION"
    HANDOFF_READINESS = "HANDOFF_READINESS"
    PRODUCTION_SERIALIZATION = "PRODUCTION_SERIALIZATION"
    REMOTE_EXECUTION_VERIFIABILITY = "REMOTE_EXECUTION_VERIFIABILITY"
    CANDIDATE_COMPLETION = "CANDIDATE_COMPLETION"
    CANDIDATE_JUDGEMENT = "CANDIDATE_JUDGEMENT"


class OperatorSource(StrEnum):
    """Authoritative evidence sources a projection may be built from.

    Every source explicitly supplied to the collector is recorded as
    present; optional sources that were absent are recorded as absent so
    a projection can never pretend to be complete.
    """

    CONTROL_PLANE_STATE = "CONTROL_PLANE_STATE"
    CONTROL_PLANE_REGISTRY = "CONTROL_PLANE_REGISTRY"
    TASK_INTENT = "TASK_INTENT"
    TASK_LINEAGE = "TASK_LINEAGE"
    AUTHORITY_BOUNDARY = "AUTHORITY_BOUNDARY"
    PARALLELIZATION_DECISION = "PARALLELIZATION_DECISION"
    WORKSPACE_IDENTITIES = "WORKSPACE_IDENTITIES"
    WORKTREE_LEASES = "WORKTREE_LEASES"
    RUN_RECORDS = "RUN_RECORDS"
    CANDIDATE_IDENTITIES = "CANDIDATE_IDENTITIES"
    CANDIDATE_RESULTS = "CANDIDATE_RESULTS"
    JUDGE_RESULT = "JUDGE_RESULT"
    VALIDATION_EVIDENCE = "VALIDATION_EVIDENCE"
    REQUALIFICATION_RESULT = "REQUALIFICATION_RESULT"
    EXECUTION_HOST_RECORDS = "EXECUTION_HOST_RECORDS"
    REMOTE_EXECUTION_LIFECYCLES = "REMOTE_EXECUTION_LIFECYCLES"
    EVENT_LOG = "EVENT_LOG"
    HANDOFF_RECORD = "HANDOFF_RECORD"
    PRODUCTION_SERIALIZATION_BARRIER = "PRODUCTION_SERIALIZATION_BARRIER"
    CURRENT_MAIN_SHA = "CURRENT_MAIN_SHA"


@dataclass(frozen=True, slots=True)
class ProjectionProvenance:
    """Explicit provenance: exactly which authoritative evidence was used."""

    repository_id: str | None
    base_sha: str | None
    cycle_id: str | None
    task_id: str | None
    node_id: str | None
    execution_epoch: int | None
    sources_present: tuple[str, ...]
    sources_absent: tuple[str, ...]
    source_counts: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "base_sha": self.base_sha,
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "execution_epoch": self.execution_epoch,
            "sources_present": list(self.sources_present),
            "sources_absent": list(self.sources_absent),
            "source_counts": [[name, count] for name, count in self.source_counts],
        }


@dataclass(frozen=True, slots=True)
class ProjectionLimits:
    """Configurable output bounds. Truncation is always disclosed."""

    max_events: int = 500
    max_workspaces: int = 50
    max_runs: int = 50
    max_candidates: int = 50
    max_blockers: int = 100
    max_artifacts: int = 50
    max_evidence_refs: int = 64

    def __post_init__(self) -> None:
        for label in (
            "max_events",
            "max_workspaces",
            "max_runs",
            "max_candidates",
            "max_blockers",
            "max_artifacts",
            "max_evidence_refs",
        ):
            value = getattr(self, label)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"ProjectionLimits.{label} must be a positive int, got {value!r}")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_events": self.max_events,
            "max_workspaces": self.max_workspaces,
            "max_runs": self.max_runs,
            "max_candidates": self.max_candidates,
            "max_blockers": self.max_blockers,
            "max_artifacts": self.max_artifacts,
            "max_evidence_refs": self.max_evidence_refs,
        }


@dataclass(frozen=True, slots=True)
class TruncationInfo:
    """Disclosed truncation record. Silent truncation is forbidden."""

    field: str
    truncated: bool
    original_count: int
    returned_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "truncated": self.truncated,
            "original_count": self.original_count,
            "returned_count": self.returned_count,
        }


@dataclass(frozen=True, slots=True)
class ProjectionHealth:
    """Overall projection health derived from evidence."""

    status: ProjectionStatus
    health: OperatorHealthState
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "health": self.health.value,
            "reason_codes": list(self.reason_codes),
        }
