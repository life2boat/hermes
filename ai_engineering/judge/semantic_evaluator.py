"""Semantic evaluation interface and deterministic implementations for candidate judging."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ai_engineering.candidates.candidate_contracts import CandidateResult
from ai_engineering.judge.judge_contracts import CandidateSemanticScore


@runtime_checkable
class SemanticCandidateEvaluator(Protocol):
    """Protocol for evaluating an eligible candidate's implementation quality."""

    def evaluate(self, candidate: CandidateResult) -> CandidateSemanticScore:
        """Evaluate candidate quality and return a bounded semantic score in [0.0, 1.0]."""
        ...


class DeterministicSemanticEvaluator:
    """Configurable deterministic semantic evaluator for testing and offline judging."""

    def __init__(
        self,
        scores_by_id: dict[str, float] | None = None,
        default_score: float = 0.8,
        evaluator_id: str = "evaluator-deterministic",
        should_fail_on: set[str] | None = None,
    ) -> None:
        self.scores_by_id = scores_by_id or {}
        self.default_score = default_score
        self.evaluator_id = evaluator_id
        self.should_fail_on = should_fail_on or set()
        self.evaluated_candidates: list[str] = []

    def evaluate(self, candidate: CandidateResult) -> CandidateSemanticScore:
        self.evaluated_candidates.append(candidate.candidate_id)
        if candidate.candidate_id in self.should_fail_on:
            raise RuntimeError(f"Simulated semantic evaluator failure for {candidate.candidate_id}")

        score = self.scores_by_id.get(candidate.candidate_id, self.default_score)
        return CandidateSemanticScore(
            candidate_id=candidate.candidate_id,
            score=score,
            rationale=f"Evaluated with deterministic score {score:.2f}",
            evaluator_id=self.evaluator_id,
        )
