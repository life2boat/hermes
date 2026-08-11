"""Typed contracts for versioned Hermes behaviour evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


BEHAVIOUR_TRACE_SCHEMA_VERSION = 1
SCENARIO_SCHEMA_VERSION = 2
SUPPORTED_SCENARIO_SCHEMA_VERSIONS = (1, 2)
BEHAVIOUR_EVAL_ENGINE_VERSION = 1
MODEL_POLICY_VERSION = 1
COST_POLICY_VERSION = 1
RATE_CARD_SCHEMA_VERSION = 1


class Status(StrEnum):
    """Normative repository status taxonomy."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"
    NOT_PERFORMED = "NOT_PERFORMED"
    UNKNOWN = "UNKNOWN"
    INCONCLUSIVE = "INCONCLUSIVE"


class TaskClass(StrEnum):
    """Closed engineering task taxonomy owned by the model policy."""

    REPOSITORY_SEARCH_LOGS = "REPOSITORY_SEARCH_LOGS"
    SMALL_PRECISE_FIX = "SMALL_PRECISE_FIX"
    BOUNDED_IMPLEMENTATION = "BOUNDED_IMPLEMENTATION"
    ARCHITECTURE = "ARCHITECTURE"
    MIGRATION_ROLLBACK_DESIGN = "MIGRATION_ROLLBACK_DESIGN"
    SECURITY_AUDIT = "SECURITY_AUDIT"
    HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION = (
        "HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION"
    )


class ReasoningLevel(StrEnum):
    """Closed reasoning taxonomy used by deterministic selection checks."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


class SubstitutionClass(StrEnum):
    """Explicit model-substitution classifications."""

    NONE = "NONE"
    ALLOWED_ALTERNATIVE = "ALLOWED_ALTERNATIVE"
    ESCALATION = "ESCALATION"
    FALLBACK = "FALLBACK"
    PROVIDER_CHANGE = "PROVIDER_CHANGE"


class LLMOpsPolicyError(ValueError):
    """Fail-closed policy/configuration error exposing only a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    policy_version: int
    task_class: TaskClass
    recommended_model: str
    allowed_alternatives: tuple[str, ...]
    recommended_reasoning: ReasoningLevel
    reason_code: str


@dataclass(frozen=True, slots=True)
class AuthorityBoundary:
    """Task authority evidence that model selection may compare, never expand."""

    allowed_effect_classes: tuple[EffectClass, ...]
    forbidden_effect_classes: tuple[EffectClass, ...]
    stop_boundary: StopBoundary
    production_authorized: bool
    secret_access_authorized: bool
    data_access_authorized: bool


@dataclass(frozen=True, slots=True)
class ModelPolicyReceipt:
    policy_version: int
    task_class: TaskClass | None
    recommended_model: str | None
    actual_model: str | None
    recommended_reasoning: ReasoningLevel | None
    actual_reasoning: ReasoningLevel | None
    substitution_class: SubstitutionClass | None
    substitution_reason_code: str | None
    authority_status: Status
    status: Status
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_model_calls: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_estimated_cost: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class UsageAccounting:
    primary_calls: int | None
    retry_calls: int | None
    judge_calls: int | None
    fallback_calls: int | None
    live_eval_calls: int | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class ModelRate:
    input_per_million_tokens: str
    output_per_million_tokens: str
    per_call: str


@dataclass(frozen=True, slots=True)
class RateCard:
    schema_version: int
    pricing_source_id: str
    currency: str
    models: tuple[tuple[str, ModelRate], ...]


@dataclass(frozen=True, slots=True)
class CostPolicyReceipt:
    policy_version: int
    budget: BudgetLimits
    usage: UsageAccounting
    total_model_calls: int | None
    model_call_status: Status
    input_token_status: Status
    output_token_status: Status
    estimated_cost_status: Status
    estimated_cost: str | None
    currency: str | None
    pricing_source_id: str | None
    rate_card_digest: str | None
    status: Status
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LLMOpsReceipt:
    model_policy_receipt: ModelPolicyReceipt
    cost_policy_receipt: CostPolicyReceipt
    overall_status: Status
