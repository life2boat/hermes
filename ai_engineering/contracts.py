"""Typed contracts for versioned Hermes behaviour evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


BEHAVIOUR_TRACE_SCHEMA_VERSION = 1
SCENARIO_SCHEMA_VERSION = 2
SUPPORTED_SCENARIO_SCHEMA_VERSIONS = (1, 2)
BEHAVIOUR_EVAL_ENGINE_VERSION = 1


class Status(StrEnum):
    """Normative repository status taxonomy."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"
    NOT_PERFORMED = "NOT_PERFORMED"
    UNKNOWN = "UNKNOWN"
    INCONCLUSIVE = "INCONCLUSIVE"


class StopBoundary(StrEnum):
    """Known engineering delivery boundaries recorded by a trace."""

    READ_ONLY = "READ_ONLY"
    LOCAL_DIFF = "LOCAL_DIFF"
    COMMIT = "COMMIT"
    DRAFT_PR = "DRAFT_PR"
    READY_PR = "READY_PR"
    MERGE = "MERGE"
    BUILD = "BUILD"
    DEPLOY = "DEPLOY"
    LIVE_SMOKE = "LIVE_SMOKE"


class EffectClass(StrEnum):
    """Small, stable taxonomy of engineering effects."""

    READ_ONLY = "READ_ONLY"
    REPOSITORY_WRITE = "REPOSITORY_WRITE"
    GIT_COMMIT = "GIT_COMMIT"
    GIT_PUSH = "GIT_PUSH"
    PR_MUTATION = "PR_MUTATION"
    PR_MERGE = "PR_MERGE"
    BUILD = "BUILD"
    DEPLOY = "DEPLOY"
    RUNTIME_MUTATION = "RUNTIME_MUTATION"
    DATA_MUTATION = "DATA_MUTATION"
    VECTOR_MUTATION = "VECTOR_MUTATION"
    SECRET_MUTATION = "SECRET_MUTATION"
    EXTERNAL_SEND = "EXTERNAL_SEND"
    OTHER_MUTATION = "OTHER_MUTATION"


class TraceValidationError(ValueError):
    """Fail-closed validation error that exposes only a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    canonical_remote: str
    base_sha: str
    head_sha: str
    branch: str
    worktree_clean: bool


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    task_class: str
    behaviour_sensitive: bool
    security_sensitive: bool
    cost_sensitive: bool
    production_sensitive: bool
    allowed_effect_classes: tuple[EffectClass, ...]
    forbidden_effect_classes: tuple[EffectClass, ...]
    stop_boundary: StopBoundary


@dataclass(frozen=True, slots=True)
class LLMEvidence:
    policy_version: str | None
    recommended_model: str | None
    actual_model: str | None
    reasoning_level: str | None


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    decision_code: str
    status: Status
    reason_code: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolEvent:
    event_id: str
    tool_name: str
    effect_class: EffectClass
    authorization_status: Status
    outcome_status: Status
    side_effect: bool
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateResult:
    gate_name: str
    required: bool
    status: Status
    evidence_refs: tuple[str, ...]


CostValue: TypeAlias = int | float | None


@dataclass(frozen=True, slots=True)
class UsageEvidence:
    model_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: CostValue


@dataclass(frozen=True, slots=True)
class TraceResult:
    status: Status


@dataclass(frozen=True, slots=True)
class BehaviourTrace:
    schema_version: int
    trace_id: str
    task_id: str
    repository: RepositoryEvidence
    task: TaskEvidence
    llm: LLMEvidence
    decisions: tuple[DecisionEvent, ...]
    tool_events: tuple[ToolEvent, ...]
    gate_results: tuple[GateResult, ...]
    usage: UsageEvidence
    result: TraceResult


@dataclass(frozen=True, slots=True)
class ScenarioAssertion:
    kind: str
    expected: str | int | bool | None


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    schema_version: int
    case_id: str
    dataset_version: str
    task_classification: str
    required_behaviour_dimensions: tuple[str, ...]
    allowed_effect_classes: tuple[EffectClass, ...]
    forbidden_effect_classes: tuple[EffectClass, ...]
    expected_stop_boundary: StopBoundary
    expected_status: Status
    sanitized_input_reference: str
    deterministic_assertions: tuple[ScenarioAssertion, ...]
    canonical_source_or_fixture_version: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    trace: BehaviourTrace
    normalized: dict[str, object]
    canonical_json: str
    digest: str



@dataclass(frozen=True, slots=True)
class AssertionResult:
    kind: str
    status: Status
    reason_code: str


@dataclass(frozen=True, slots=True)
class GraderResult:
    grader: str
    status: Status
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    category: str
    critical: bool
    status: Status
    observed_status: Status
    assertion_results: tuple[AssertionResult, ...]
    grader_results: tuple[GraderResult, ...]
    trace_digest: str
    dataset_version: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetResult:
    category: str
    critical: bool
    status: Status
    total: int
    passed: int
    failed: int
    blocked: int
    cases: tuple[CaseResult, ...]


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    engine_version: int
    dataset_version: str
    status: Status
    total_cases: int
    passed: int
    failed: int
    blocked: int
    critical_total: int
    critical_passed: int
    critical_failed: int
    datasets: tuple[DatasetResult, ...]
    baseline_status: Status
    corpus_digest: str
