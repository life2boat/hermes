"""Strongly typed contracts for Parallelization Strategy, Decision, and Concurrency Budget."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ai_engineering.contracts import (
    AuthorityBoundary,
    EffectClass,
    ReasoningLevel,
    TaskClass,
)
from ai_engineering.task_intent import TaskIntent

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

PARALLELIZATION_POLICY_VERSION = 1
PARALLELIZATION_DECISION_SCHEMA_VERSION = 1
DEFAULT_MAX_CANDIDATES = 3
GLOBAL_MAX_CANDIDATES_LIMIT = 3


class ParallelizationStrategy(StrEnum):
    """Normative parallelization strategies supported by the Hermes execution plane."""

    NONE = "NONE"
    PREPARATORY = "PREPARATORY"
    CANDIDATE = "CANDIDATE"
    REVIEW = "REVIEW"
    REHEARSAL = "REHEARSAL"


class ParallelizationBlockingReason(StrEnum):
    """Deterministic machine-readable reason codes for parallelization policy blocks."""

    PARALLEL_MUTATION_CONFLICT = "PARALLEL_MUTATION_CONFLICT"
    PARALLELIZATION_NOT_AUTHORIZED = "PARALLELIZATION_NOT_AUTHORIZED"
    PARALLELIZATION_BUDGET_EXCEEDED = "PARALLELIZATION_BUDGET_EXCEEDED"
    PARALLELIZATION_INPUT_INVALID = "PARALLELIZATION_INPUT_INVALID"
    PARALLELIZATION_STRATEGY_INVALID = "PARALLELIZATION_STRATEGY_INVALID"
    UNKNOWN_TASK_CLASSIFICATION = "UNKNOWN_TASK_CLASSIFICATION"


class ParallelizationPolicyError(ValueError):
    """Fail-closed error for parallelization policy and budget contract violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class ConcurrencyBudget:
    """Explicit, bounded limits for concurrent agents, worktrees, processes, and model calls."""

    max_agents: int = 3
    max_worktrees: int = 3
    max_remote_processes: int = 0
    max_llm_calls: int = 30
    max_candidates: int = DEFAULT_MAX_CANDIDATES

    def __post_init__(self) -> None:
        if not isinstance(self.max_agents, int) or self.max_agents < 1:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_agents must be integer >= 1, got {self.max_agents!r}",
            )
        if not isinstance(self.max_worktrees, int) or self.max_worktrees < 1:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_worktrees must be integer >= 1, got {self.max_worktrees!r}",
            )
        if not isinstance(self.max_remote_processes, int) or self.max_remote_processes < 0:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_remote_processes must be integer >= 0, got {self.max_remote_processes!r}",
            )
        if not isinstance(self.max_llm_calls, int) or self.max_llm_calls < 1:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_llm_calls must be integer >= 1, got {self.max_llm_calls!r}",
            )
        if not isinstance(self.max_candidates, int) or self.max_candidates < 1 or self.max_candidates > GLOBAL_MAX_CANDIDATES_LIMIT:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_candidates must be between 1 and {GLOBAL_MAX_CANDIDATES_LIMIT}, got {self.max_candidates!r}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize budget to canonical dictionary."""
        return {
            "max_agents": self.max_agents,
            "max_worktrees": self.max_worktrees,
            "max_remote_processes": self.max_remote_processes,
            "max_llm_calls": self.max_llm_calls,
            "max_candidates": self.max_candidates,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ConcurrencyBudget:
        """Deserialize budget from dictionary."""
        return cls(
            max_agents=int(payload.get("max_agents", 3)),
            max_worktrees=int(payload.get("max_worktrees", 3)),
            max_remote_processes=int(payload.get("max_remote_processes", 0)),
            max_llm_calls=int(payload.get("max_llm_calls", 30)),
            max_candidates=int(payload.get("max_candidates", DEFAULT_MAX_CANDIDATES)),
        )


@dataclass(frozen=True, slots=True)
class ParallelizationDecision:
    """Deterministic, immutable parallelization decision produced by the ParallelizationPolicy."""

    allowed: bool
    strategy: ParallelizationStrategy
    max_candidates: int
    max_agents: int
    requires_single_mutation_owner: bool
    requires_serialization_barrier: bool
    reason: str
    blockers: tuple[str, ...] = ()
    policy_version: int = PARALLELIZATION_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_INPUT_INVALID.value,
                "allowed must be boolean",
            )
        if not isinstance(self.strategy, ParallelizationStrategy):
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value,
                f"Invalid strategy: {self.strategy!r}",
            )
        if not isinstance(self.max_candidates, int) or self.max_candidates < 0 or self.max_candidates > GLOBAL_MAX_CANDIDATES_LIMIT:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_candidates must be between 0 and {GLOBAL_MAX_CANDIDATES_LIMIT}, got {self.max_candidates!r}",
            )
        if not isinstance(self.max_agents, int) or self.max_agents < 1:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"max_agents must be >= 1, got {self.max_agents!r}",
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_INPUT_INVALID.value,
                "reason must be non-empty string",
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize decision to canonical dictionary."""
        return {
            "schema_version": PARALLELIZATION_DECISION_SCHEMA_VERSION,
            "policy_version": self.policy_version,
            "allowed": self.allowed,
            "strategy": self.strategy.value,
            "max_candidates": self.max_candidates,
            "max_agents": self.max_agents,
            "requires_single_mutation_owner": self.requires_single_mutation_owner,
            "requires_serialization_barrier": self.requires_serialization_barrier,
            "reason": self.reason,
            "blockers": list(self.blockers),
        }

    def to_json(self) -> str:
        """Serialize to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ParallelizationDecision:
        """Deserialize decision from dictionary."""
        if not isinstance(payload, Mapping):
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_INPUT_INVALID.value,
                "Payload must be a mapping",
            )
        raw_strat = payload.get("strategy")
        try:
            strategy = ParallelizationStrategy(str(raw_strat))
        except ValueError as exc:
            raise ParallelizationPolicyError(
                ParallelizationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value,
                f"Unknown strategy: {raw_strat!r}",
            ) from exc

        return cls(
            allowed=bool(payload.get("allowed", False)),
            strategy=strategy,
            max_candidates=int(payload.get("max_candidates", 1)),
            max_agents=int(payload.get("max_agents", 1)),
            requires_single_mutation_owner=bool(payload.get("requires_single_mutation_owner", False)),
            requires_serialization_barrier=bool(payload.get("requires_serialization_barrier", False)),
            reason=str(payload.get("reason", "")),
            blockers=tuple(payload.get("blockers") or ()),
            policy_version=int(payload.get("policy_version", PARALLELIZATION_POLICY_VERSION)),
        )

    @classmethod
    def from_json(cls, raw: str) -> ParallelizationDecision:
        """Deserialize from JSON string."""
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise ParallelizationPolicyError("INVALID_JSON", f"Could not parse JSON: {exc}") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class ParallelizationPolicyInput:
    """Normalized input contract for parallelization policy evaluations."""

    task_intent: TaskIntent | None = None
    task_class: TaskClass | None = None
    task_complexity: ReasoningLevel | str = ReasoningLevel.MEDIUM
    task_uncertainty: str = "LOW"  # LOW, MEDIUM, HIGH
    implementation_ambiguity: str = "LOW"  # LOW, MEDIUM, HIGH
    production_risk: str = "LOW"  # LOW, MEDIUM, HIGH, CRITICAL
    side_effects: tuple[EffectClass, ...] = (EffectClass.REPOSITORY_WRITE,)
    authority_boundary: AuthorityBoundary | None = None
    estimated_cost: float = 0.0
    expected_information_gain: str = "MEDIUM"  # LOW, MEDIUM, HIGH
    budget: ConcurrencyBudget = field(default_factory=ConcurrencyBudget)
