"""Candidate implementations foundation package for parallel workspaces."""

from ai_engineering.candidates.candidate_contracts import (
    CandidateBatchAggregate,
    CandidateBlockingReason,
    CandidateError,
    CandidateIdentity,
    CandidateImplementationBatch,
    CandidateImplementationRequest,
    CandidateResult,
    CandidateState,
    ValidationCommandResult,
)
from ai_engineering.candidates.candidate_runner import (
    ParallelCandidateRunner,
    execute_single_candidate,
    make_candidate_branch_name,
    validate_candidate_command,
)

__all__ = [
    "CandidateBatchAggregate",
    "CandidateBlockingReason",
    "CandidateError",
    "CandidateIdentity",
    "CandidateImplementationBatch",
    "CandidateImplementationRequest",
    "CandidateResult",
    "CandidateState",
    "ParallelCandidateRunner",
    "ValidationCommandResult",
    "execute_single_candidate",
    "make_candidate_branch_name",
    "validate_candidate_command",
]
