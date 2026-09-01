"""Hermes Parallel Repository Investigation package."""

from ai_engineering.investigation.investigation_contracts import (
    INVESTIGATION_CONTRACT_VERSION,
    INVESTIGATION_RESULT_SCHEMA_VERSION,
    MAX_SNIPPET_LENGTH,
    InvestigationBlockingReason,
    InvestigationError,
    RepositoryInvestigationAggregate,
    RepositoryInvestigationBatch,
    RepositoryInvestigationRequest,
    RepositoryInvestigationResult,
    RepositoryMatch,
)
from ai_engineering.investigation.investigation_runner import (
    ParallelRepositoryInvestigator,
    execute_single_investigation,
    validate_investigation_command,
)

__all__ = [
    "INVESTIGATION_CONTRACT_VERSION",
    "INVESTIGATION_RESULT_SCHEMA_VERSION",
    "MAX_SNIPPET_LENGTH",
    "InvestigationBlockingReason",
    "InvestigationError",
    "ParallelRepositoryInvestigator",
    "RepositoryInvestigationAggregate",
    "RepositoryInvestigationBatch",
    "RepositoryInvestigationRequest",
    "RepositoryInvestigationResult",
    "RepositoryMatch",
    "execute_single_investigation",
    "validate_investigation_command",
]
