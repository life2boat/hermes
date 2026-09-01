"""Hermes Operator Observability Plane (PR-12).

Deterministic, read-only projection over authoritative PR-1..PR-11.1
contracts. Observability owns projection only — it never creates,
mutates, repairs, or authorizes control-plane state.

Public API surface:

- :func:`collect_operator_snapshot` — read-only collector.
- :class:`OperatorSnapshot` — immutable versioned snapshot contract.
- :class:`OperatorQueries` — pure read-only query facade.
- :func:`canonical_json` / :func:`human_summary` — deterministic rendering.
- :func:`load_operator_snapshot_dict` — fail-closed snapshot loading.
"""

from __future__ import annotations

from ai_engineering.observability.contracts import (
    OBSERVABILITY_CONTRACT_VERSION,
    OBSERVABILITY_SCHEMA_VERSION,
    BarrierName,
    ObservabilityReasonCode,
    OperatorHealthState,
    OperatorSource,
    ProjectionHealth,
    ProjectionLimits,
    ProjectionProvenance,
    ProjectionStatus,
    TruncationInfo,
)
from ai_engineering.observability.collector import OperatorQueries, collect_operator_snapshot
from ai_engineering.observability.projection import OperatorSnapshot
from ai_engineering.observability.redaction import redact_operator_dict
from ai_engineering.observability.rendering import (
    ObservabilitySchemaError,
    canonical_json,
    human_summary,
    load_operator_snapshot_dict,
)

__all__ = [
    "OBSERVABILITY_CONTRACT_VERSION",
    "OBSERVABILITY_SCHEMA_VERSION",
    "BarrierName",
    "ObservabilityReasonCode",
    "ObservabilitySchemaError",
    "OperatorHealthState",
    "OperatorQueries",
    "OperatorSnapshot",
    "OperatorSource",
    "ProjectionHealth",
    "ProjectionLimits",
    "ProjectionProvenance",
    "ProjectionStatus",
    "TruncationInfo",
    "canonical_json",
    "collect_operator_snapshot",
    "human_summary",
    "load_operator_snapshot_dict",
    "redact_operator_dict",
]
