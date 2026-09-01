"""Immutable strongly-typed contracts and blockers for Candidate Requalification and Main Drift."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ai_engineering.candidates.candidate_contracts import CandidateResult
from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    WorkspaceSnapshot,
    validate_repository_relative_path,
)

_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)

REQUALIFICATION_CONTRACT_VERSION = "4.1.0"


class BaseRelationship(str, Enum):
    """Deterministic relationship between candidate base SHA and current canonical main SHA."""

    EXACT_BASE = "EXACT_BASE"
    MAIN_ADVANCED_DESCENDANT = "MAIN_ADVANCED_DESCENDANT"
    MAIN_DIVERGED = "MAIN_DIVERGED"
    BASE_NOT_ANCESTOR = "BASE_NOT_ANCESTOR"
    BASE_UNKNOWN = "BASE_UNKNOWN"
    INVALID = "INVALID"


class RequalificationDecisionState(str, Enum):
    """Deterministic decision outcome for candidate requalification."""

    NO_REQUALIFICATION_REQUIRED = "NO_REQUALIFICATION_REQUIRED"
    REQUALIFIED = "REQUALIFIED"
    REQUALIFICATION_REQUIRED = "REQUALIFICATION_REQUIRED"
    REQUALIFICATION_REJECTED = "REQUALIFICATION_REJECTED"
    NEW_CANDIDATE_REQUIRED = "NEW_CANDIDATE_REQUIRED"
    FAILED = "FAILED"


class ValidationFreshness(str, Enum):
    """Classification of historical candidate validation results against advanced base."""

    STILL_APPLICABLE = "STILL_APPLICABLE"
    REQUIRES_RERUN = "REQUIRES_RERUN"
    INVALID = "INVALID"


class JudgementFreshness(str, Enum):
    """Classification of CandidateJudgeResult freshness against current canonical main."""

    CURRENT = "CURRENT"
    STALE_BASE = "STALE_BASE"
    INVALID = "INVALID"


class RequalificationBlockingReason(str, Enum):
    """Machine-readable reason codes for requalification and main drift safety."""

    CANDIDATE_BASE_DRIFT = "CANDIDATE_BASE_DRIFT"
    CANDIDATE_REQUALIFICATION_REQUIRED = "CANDIDATE_REQUALIFICATION_REQUIRED"
    CANDIDATE_REQUALIFICATION_REJECTED = "CANDIDATE_REQUALIFICATION_REJECTED"
    CANDIDATE_DRIFT_OVERLAP = "CANDIDATE_DRIFT_OVERLAP"
    CANDIDATE_DRIFT_UNRESOLVABLE = "CANDIDATE_DRIFT_UNRESOLVABLE"
    CANDIDATE_VALIDATION_STALE = "CANDIDATE_VALIDATION_STALE"
    CANDIDATE_JUDGEMENT_STALE = "CANDIDATE_JUDGEMENT_STALE"
    # Reused architectural blockers
    DIFF_ARTIFACT_DIGEST_MISMATCH = "DIFF_ARTIFACT_DIGEST_MISMATCH"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"
    CANDIDATE_RESULT_INVALID = "CANDIDATE_RESULT_INVALID"
    REQUALIFICATION_COLLISION = "REQUALIFICATION_COLLISION"
    REQUALIFICATION_PATH_INVALID = "REQUALIFICATION_PATH_INVALID"


class RequalificationError(Exception):
    """Fail-closed exception for requalification contract and invariant violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DriftEvidence:
    """Normalized evidence of changes in canonical main since candidate creation."""

    candidate_base_sha: str
    current_main_sha: str
    changed_paths: tuple[str, ...]
    diff_stat: str
    diff_digest: str
    drift_commit_count: int

    def __post_init__(self) -> None:
        if not _HEX_40_RE.match(self.candidate_base_sha):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,
                f"Invalid candidate_base_sha: {self.candidate_base_sha!r}",
            )
        if not _HEX_40_RE.match(self.current_main_sha):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,
                f"Invalid current_main_sha: {self.current_main_sha!r}",
            )
        if not isinstance(self.changed_paths, tuple):
            object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        for p in self.changed_paths:
            validate_repository_relative_path(p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_base_sha": self.candidate_base_sha.lower(),
            "current_main_sha": self.current_main_sha.lower(),
            "changed_paths": list(self.changed_paths),
            "diff_stat": self.diff_stat,
            "diff_digest": self.diff_digest.lower(),
            "drift_commit_count": self.drift_commit_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DriftEvidence:
        return cls(
            candidate_base_sha=str(data["candidate_base_sha"]).lower(),
            current_main_sha=str(data["current_main_sha"]).lower(),
            changed_paths=tuple(data.get("changed_paths", ())),
            diff_stat=str(data.get("diff_stat", "")),
            diff_digest=str(data["diff_digest"]).lower(),
            drift_commit_count=int(data.get("drift_commit_count", 0)),
        )


@dataclass(frozen=True, slots=True)
class RequalificationEvidence:
    """Comprehensive evidence of base drift analysis and overlap comparison."""

    candidate_base_sha: str
    current_main_sha: str
    drift_changed_paths: tuple[str, ...]
    candidate_changed_paths: tuple[str, ...]
    overlapping_paths: tuple[str, ...]
    drift_diff_digest: str
    candidate_diff_digest: str
    validation_status: ValidationFreshness

    def __post_init__(self) -> None:
        if not isinstance(self.drift_changed_paths, tuple):
            object.__setattr__(self, "drift_changed_paths", tuple(self.drift_changed_paths))
        if not isinstance(self.candidate_changed_paths, tuple):
            object.__setattr__(self, "candidate_changed_paths", tuple(self.candidate_changed_paths))
        if not isinstance(self.overlapping_paths, tuple):
            object.__setattr__(self, "overlapping_paths", tuple(self.overlapping_paths))

        for p in self.drift_changed_paths:
            validate_repository_relative_path(p)
        for p in self.candidate_changed_paths:
            validate_repository_relative_path(p)
        for p in self.overlapping_paths:
            validate_repository_relative_path(p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_base_sha": self.candidate_base_sha.lower(),
            "current_main_sha": self.current_main_sha.lower(),
            "drift_changed_paths": list(self.drift_changed_paths),
            "candidate_changed_paths": list(self.candidate_changed_paths),
            "overlapping_paths": list(self.overlapping_paths),
            "drift_diff_digest": self.drift_diff_digest.lower(),
            "candidate_diff_digest": self.candidate_diff_digest.lower(),
            "validation_status": self.validation_status.value if isinstance(self.validation_status, ValidationFreshness) else str(self.validation_status),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RequalificationEvidence:
        v_status = ValidationFreshness(data["validation_status"]) if isinstance(data["validation_status"], str) else data["validation_status"]
        return cls(
            candidate_base_sha=str(data["candidate_base_sha"]).lower(),
            current_main_sha=str(data["current_main_sha"]).lower(),
            drift_changed_paths=tuple(data.get("drift_changed_paths", ())),
            candidate_changed_paths=tuple(data.get("candidate_changed_paths", ())),
            overlapping_paths=tuple(data.get("overlapping_paths", ())),
            drift_diff_digest=str(data.get("drift_diff_digest", "")).lower(),
            candidate_diff_digest=str(data.get("candidate_diff_digest", "")).lower(),
            validation_status=v_status,
        )


@dataclass(frozen=True, slots=True)
class CandidateRequalificationRequest:
    """Request envelope for evaluating candidate validity against advanced main."""

    requalification_id: str
    task_id: str
    node_id: str
    candidate_id: str
    workspace_id: str
    run_id: str
    candidate_base_sha: str
    current_main_sha: str
    candidate_result: CandidateResult
    snapshot_evidence: WorkspaceSnapshot | DiffArtifact | None = None
    requested_at: str = ""

    def __post_init__(self) -> None:
        if not self.requalification_id or not _IDENTIFIER_RE.match(self.requalification_id):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid requalification_id: {self.requalification_id!r}",
            )
        if not self.candidate_id or not _IDENTIFIER_RE.match(self.candidate_id):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        if not _HEX_40_RE.match(self.candidate_base_sha):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,
                f"Invalid candidate_base_sha: {self.candidate_base_sha!r}",
            )
        if not _HEX_40_RE.match(self.current_main_sha):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,
                f"Invalid current_main_sha: {self.current_main_sha!r}",
            )
        if not isinstance(self.candidate_result, CandidateResult):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                "candidate_result must be an instance of CandidateResult",
            )
        if self.candidate_result.candidate_id != self.candidate_id:
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"candidate_result.candidate_id ({self.candidate_result.candidate_id}) != {self.candidate_id}",
            )
        if self.candidate_result.base_sha.lower() != self.candidate_base_sha.lower():
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,
                f"candidate_result.base_sha ({self.candidate_result.base_sha}) != {self.candidate_base_sha}",
            )


@dataclass(frozen=True, slots=True)
class CandidateRequalificationResult:
    """Deterministic evaluation outcome of candidate requalification."""

    requalification_id: str
    candidate_id: str
    candidate_base_sha: str
    current_main_sha: str
    relationship: BaseRelationship
    decision_state: RequalificationDecisionState
    eligible: bool
    requires_new_candidate: bool
    blockers: tuple[str, ...]
    evidence: RequalificationEvidence | None
    completed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.relationship, BaseRelationship):
            try:
                object.__setattr__(self, "relationship", BaseRelationship(str(self.relationship)))
            except ValueError as exc:
                raise RequalificationError(
                    RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                    f"Invalid relationship: {self.relationship!r}",
                ) from exc

        if not isinstance(self.decision_state, RequalificationDecisionState):
            try:
                object.__setattr__(self, "decision_state", RequalificationDecisionState(str(self.decision_state)))
            except ValueError as exc:
                raise RequalificationError(
                    RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                    f"Invalid decision_state: {self.decision_state!r}",
                ) from exc

        if not isinstance(self.blockers, tuple):
            object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REQUALIFICATION_CONTRACT_VERSION,
            "requalification_id": self.requalification_id,
            "candidate_id": self.candidate_id,
            "candidate_base_sha": self.candidate_base_sha.lower(),
            "current_main_sha": self.current_main_sha.lower(),
            "relationship": self.relationship.value,
            "decision_state": self.decision_state.value,
            "eligible": self.eligible,
            "requires_new_candidate": self.requires_new_candidate,
            "blockers": list(self.blockers),
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "completed_at": self.completed_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CandidateRequalificationResult:
        rel = BaseRelationship(data["relationship"]) if isinstance(data["relationship"], str) else data["relationship"]
        dec = RequalificationDecisionState(data["decision_state"]) if isinstance(data["decision_state"], str) else data["decision_state"]
        ev = RequalificationEvidence.from_dict(data["evidence"]) if data.get("evidence") else None
        return cls(
            requalification_id=str(data["requalification_id"]),
            candidate_id=str(data["candidate_id"]),
            candidate_base_sha=str(data["candidate_base_sha"]).lower(),
            current_main_sha=str(data["current_main_sha"]).lower(),
            relationship=rel,
            decision_state=dec,
            eligible=bool(data["eligible"]),
            requires_new_candidate=bool(data["requires_new_candidate"]),
            blockers=tuple(data.get("blockers", ())),
            evidence=ev,
            completed_at=str(data["completed_at"]),
        )

    @classmethod
    def from_json(cls, raw: str) -> CandidateRequalificationResult:
        return cls.from_dict(json.loads(raw))
