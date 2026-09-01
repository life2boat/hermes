"""Candidate judging foundation package."""

from ai_engineering.judge.candidate_judge import CandidateJudge
from ai_engineering.judge.judge_contracts import (
    CANDIDATE_JUDGE_CONTRACT_VERSION,
    JUDGE_RESULT_SCHEMA_VERSION,
    CandidateDecisionState,
    CandidateHardGateResult,
    CandidateJudgeError,
    CandidateJudgeRequest,
    CandidateJudgeResult,
    CandidateJudgement,
    CandidateSemanticScore,
    JudgeBlockingReason,
    JudgeEventType,
)
from ai_engineering.judge.semantic_evaluator import (
    DeterministicSemanticEvaluator,
    SemanticCandidateEvaluator,
)

__all__ = [
    "CANDIDATE_JUDGE_CONTRACT_VERSION",
    "JUDGE_RESULT_SCHEMA_VERSION",
    "CandidateDecisionState",
    "CandidateHardGateResult",
    "CandidateJudge",
    "CandidateJudgeError",
    "CandidateJudgeRequest",
    "CandidateJudgeResult",
    "CandidateJudgement",
    "CandidateSemanticScore",
    "DeterministicSemanticEvaluator",
    "JudgeBlockingReason",
    "JudgeEventType",
    "SemanticCandidateEvaluator",
]
