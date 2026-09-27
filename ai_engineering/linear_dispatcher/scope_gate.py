"""Execution policy and forbidden scope gate for Hermes Autonomous Dispatcher."""

from __future__ import annotations

import re
from typing import Sequence

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    LinearTask,
)

# Forbidden keyword patterns in issue description or task instructions
_FORBIDDEN_PROD_DEPLOY_PATTERNS = re.compile(
    r"\b(production\s+deploy|deploy\s+to\s+production|restart\s+production|deploy/host-secrets|/etc/hermes)\b",
    re.IGNORECASE,
)
_FORBIDDEN_DB_MIGRATION_PATTERNS = re.compile(
    r"\b(alembic\s+upgrade|apply\s+migration|migrate\s+production\s+db|alter\s+table|drop\s+table)\b",
    re.IGNORECASE,
)
_FORBIDDEN_QDRANT_PATTERNS = re.compile(
    r"\b(qdrant\s+mutation|delete\s+collection|recreate\s+collection|delete\s+points)\b",
    re.IGNORECASE,
)
_FORBIDDEN_CREDENTIALS_PATTERNS = re.compile(
    r"\b(mutate\s+credentials|rotate\s+secret|new\s+token|\.env\.production|host-secrets\.env)\b",
    re.IGNORECASE,
)
_FORBIDDEN_SECURITY_POLICY_PATTERNS = re.compile(
    r"\b(public_access\s*=\s*true|allowlist_count\s*>\s*5|disable\s+auth|bypass\s+auth)\b",
    re.IGNORECASE,
)
_FORBIDDEN_DESTRUCTIVE_COMMANDS = re.compile(
    r"\b(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fdx|force\s+push|--force)\b",
    re.IGNORECASE,
)
_FORBIDDEN_AUTO_MERGE_PATTERNS = re.compile(
    r"\b(auto-merge|auto_merge|merge\s+to\s+main|merge\s+pr)\b",
    re.IGNORECASE,
)

# Forbidden file paths (never allowed to be modified autonomously)
_FORBIDDEN_FILE_PATTERNS = [
    re.compile(r"^deploy/", re.IGNORECASE),
    re.compile(r"^production/", re.IGNORECASE),
    re.compile(r"^credentials/", re.IGNORECASE),
    re.compile(r"^secrets/", re.IGNORECASE),
    re.compile(r"\.env($|\.)", re.IGNORECASE),
    re.compile(r"^gateway/migrations/", re.IGNORECASE),
]


class ScopeGate:
    """Evaluates requested tasks and proposed file changes against safety invariants."""

    @staticmethod
    def evaluate_task_intent(task: LinearTask) -> tuple[bool, BlockReasonCode | None]:
        """Inspect task title, description, and metadata for forbidden intent."""
        text = f"{task.title}\n{task.description}"

        if _FORBIDDEN_PROD_DEPLOY_PATTERNS.search(text):
            return False, BlockReasonCode.PRODUCTION_MUTATION_FORBIDDEN

        if _FORBIDDEN_DB_MIGRATION_PATTERNS.search(text):
            return False, BlockReasonCode.DB_MIGRATION_FORBIDDEN

        if _FORBIDDEN_QDRANT_PATTERNS.search(text):
            return False, BlockReasonCode.QDRANT_MUTATION_FORBIDDEN

        if _FORBIDDEN_CREDENTIALS_PATTERNS.search(text):
            return False, BlockReasonCode.CREDENTIALS_MUTATION_FORBIDDEN

        if _FORBIDDEN_SECURITY_POLICY_PATTERNS.search(text):
            return False, BlockReasonCode.SECURITY_POLICY_FORBIDDEN

        if _FORBIDDEN_DESTRUCTIVE_COMMANDS.search(text):
            return False, BlockReasonCode.DESTRUCTIVE_COMMAND_FORBIDDEN

        if _FORBIDDEN_AUTO_MERGE_PATTERNS.search(text):
            return False, BlockReasonCode.AUTO_MERGE_FORBIDDEN

        return True, None

    @staticmethod
    def evaluate_changed_files(changed_files: Sequence[str]) -> tuple[bool, BlockReasonCode | None]:
        """Check if any changed file falls into forbidden scope."""
        for path in changed_files:
            norm_path = path.strip().replace("\\", "/")
            for pat in _FORBIDDEN_FILE_PATTERNS:
                if pat.search(norm_path):
                    if "migration" in norm_path.lower():
                        return False, BlockReasonCode.DB_MIGRATION_FORBIDDEN
                    if "secret" in norm_path.lower() or ".env" in norm_path.lower():
                        return False, BlockReasonCode.CREDENTIALS_MUTATION_FORBIDDEN
                    return False, BlockReasonCode.PRODUCTION_MUTATION_FORBIDDEN
        return True, None
