"""Contracts and data models for Hermes Linear Autonomous Dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


from typing import Any, Mapping, Sequence


class TaskState(StrEnum):
    DISCOVERED = "DISCOVERED"
    CLAIMED = "CLAIMED"
    WORKTREE_READY = "WORKTREE_READY"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    COMMITTED = "COMMITTED"
    BRANCH_PUSHED = "BRANCH_PUSHED"
    PR_OPEN = "PR_OPEN"
    CI_PENDING = "CI_PENDING"
    CI_PASS = "CI_PASS"
    WRITEBACK_DONE = "WRITEBACK_DONE"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class BlockReasonCode(StrEnum):
    MISSING_REQUIRED_LABEL = "MISSING_REQUIRED_LABEL"
    MISSING_AGENT_AUTO_LABEL = "MISSING_AGENT_AUTO_LABEL"
    MISSING_AGENT_SHADOW_LABEL = "MISSING_AGENT_SHADOW_LABEL"
    PRODUCTION_MUTATION_FORBIDDEN = "PRODUCTION_MUTATION_FORBIDDEN"
    DB_MIGRATION_FORBIDDEN = "DB_MIGRATION_FORBIDDEN"
    SCHEMA_BREAKING_FORBIDDEN = "SCHEMA_BREAKING_FORBIDDEN"
    QDRANT_MUTATION_FORBIDDEN = "QDRANT_MUTATION_FORBIDDEN"
    CREDENTIALS_MUTATION_FORBIDDEN = "CREDENTIALS_MUTATION_FORBIDDEN"
    SECURITY_POLICY_FORBIDDEN = "SECURITY_POLICY_FORBIDDEN"
    DESTRUCTIVE_COMMAND_FORBIDDEN = "DESTRUCTIVE_COMMAND_FORBIDDEN"
    FORCE_PUSH_FORBIDDEN = "FORCE_PUSH_FORBIDDEN"
    AUTO_MERGE_FORBIDDEN = "AUTO_MERGE_FORBIDDEN"
    DIRTY_CANONICAL_CHECKOUT = "DIRTY_CANONICAL_CHECKOUT"
    ACTIVE_FOREIGN_LEASE = "ACTIVE_FOREIGN_LEASE"
    LOST_LEASE = "LOST_LEASE"
    CI_SHA_MISMATCH = "CI_SHA_MISMATCH"
    CI_FAILED = "CI_FAILED"
    REREAD_VERIFICATION_FAILED = "REREAD_VERIFICATION_FAILED"
    NO_CHANGES_PRODUCED = "NO_CHANGES_PRODUCED"
    CANONICAL_MAIN_DRIFT = "CANONICAL_MAIN_DRIFT"
    CANONICAL_MAIN_LOOKUP_FAILED = "CANONICAL_MAIN_LOOKUP_FAILED"
    RECOVERY_BLOCKED = "RECOVERY_BLOCKED"
    NO_SUPPORTED_TASK_EXECUTION_BACKEND = "NO_SUPPORTED_TASK_EXECUTION_BACKEND"
    CANONICAL_ROOT_INVALID = "CANONICAL_ROOT_INVALID"
    STALE_WORKTREE_DIRTY = "STALE_WORKTREE_DIRTY"
    UNKNOWN_BASE_COMMIT = "UNKNOWN_BASE_COMMIT"


@dataclass(frozen=True, slots=True)
class LinearTask:
    id: str
    uuid: str
    title: str
    description: str
    state: str
    priority: int
    assignee: str | None
    labels: tuple[str, ...]
    created_at: str
    updated_at: str
    url: str

    @property
    def identifier(self) -> str:
        return self.id


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    task_id: str
    claim_owner: str
    claim_token: str
    claimed_at: str
    lease_expires_at: str

    def is_active(self, now_iso: str | None = None) -> bool:
        if now_iso is None:
            now_dt = datetime.now(timezone.utc)
        else:
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        expires_dt = datetime.fromisoformat(
            self.lease_expires_at.replace("Z", "+00:00")
        )
        return now_dt < expires_dt


@dataclass(frozen=True, slots=True)
class StateTransitionRecord:
    task_id: str
    from_state: TaskState
    to_state: TaskState
    transitioned_at: str
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DispatcherConfig:
    canonical_repo: str = "life2boat/hermes"
    canonical_remote: str = "github"
    canonical_main_ref: str = "refs/remotes/github/main"
    worker_id: str = "hermes-autonomous-dispatcher-v1"
    lease_duration_sec: int = 1800
    require_auto_label: bool = True
    require_shadow_label: bool = True
    auto_merge_allowed: bool = False
    production_mutation_allowed: bool = False
    ci_poll_interval_sec: int = 15
    ci_max_wait_sec: int = 600
    enabled: bool = False
    mode: str = "shadow"
