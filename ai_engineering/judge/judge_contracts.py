"""Typed contracts for deterministic candidate judging and evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
import re
from typing import Any

from ai_engineering.candidates.candidate_contracts import CandidateResult, CandidateState

_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

CANDIDATE_JUDGE_CONTRACT_VERSION = "4.1.0"
JUDGE_RESULT_SCHEMA_VERSION = "4.1.0"


class JudgeBlockingReason(str, Enum):
    """Machine-readable blockers for candidate judging."""

    CANDIDATE_HARD_VALIDATION_FAILED = "CANDIDATE_HARD_VALIDATION_FAILED"
    CANDIDATE_BASE_DRIFT = "CANDIDATE_BASE_DRIFT"
    CANDIDATE_SEMANTIC_SCORE_INVALID = "CANDIDATE_SEMANTIC_SCORE_INVALID"
    CANDIDATE_SEMANTIC_EVALUATION_FAILED = "CANDIDATE_SEMANTIC_EVALUATION_FAILED"
    CANDIDATE_JUDGE_INPUT_INVALID = "CANDIDATE_JUDGE_INPUT_INVALID"
    CANDIDATE_JUDGE_NO_ELIGIBLE = "CANDIDATE_JUDGE_NO_ELIGIBLE"
    CANDIDATE_JUDGE_TIE = "CANDIDATE_JUDGE_TIE"
    CANDIDATE_RESULT_INVALID = "CANDIDATE_RESULT_INVALID"
    CANDIDATE_ID_COLLISION = "CANDIDATE_ID_COLLISION"
    CANDIDATE_BASE_SHA_MISMATCH = "CANDIDATE_BASE_SHA_MISMATCH"
    CANDIDATE_SCOPE_VIOLATION = "CANDIDATE_SCOPE_VIOLATION"
    CANDIDATE_PATH_ESCAPE = "CANDIDATE_PATH_ESCAPE"
    CANDIDATE_NOT_AUTHORIZED = "CANDIDATE_NOT_AUTHORIZED"
    CANDIDATE_MAIN_WORKTREE_FORBIDDEN = "CANDIDATE_MAIN_WORKTREE_FORBIDDEN"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"


class CandidateDecisionState(str, Enum):
    """Deterministic states for candidate judge outcome."""

    NO_CANDIDATES = "NO_CANDIDATES"
    NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
    SINGLE_ELIGIBLE = "SINGLE_ELIGIBLE"
    RANKED_SELECTION = "RANKED_SELECTION"
    TIE = "TIE"
    JUDGE_FAILED = "JUDGE_FAILED"


class JudgeEventType(str, Enum):
    """Internal observability events for candidate judge execution."""

    CANDIDATE_JUDGE_STARTED = "CANDIDATE_JUDGE_STARTED"
    CANDIDATE_HARD_GATE_PASSED = "CANDIDATE_HARD_GATE_PASSED"
    CANDIDATE_HARD_GATE_FAILED = "CANDIDATE_HARD_GATE_FAILED"
    CANDIDATE_SEMANTIC_REVIEW_STARTED = "CANDIDATE_SEMANTIC_REVIEW_STARTED"
    CANDIDATE_SEMANTIC_REVIEW_COMPLETED = "CANDIDATE_SEMANTIC_REVIEW_COMPLETED"
    CANDIDATE_SELECTED = "CANDIDATE_SELECTED"
    CANDIDATE_JUDGE_NO_ELIGIBLE = "CANDIDATE_JUDGE_NO_ELIGIBLE"
    CANDIDATE_JUDGE_FAILED = "CANDIDATE_JUDGE_FAILED"
    CANDIDATE_JUDGE_COMPLETED = "CANDIDATE_JUDGE_COMPLETED"


class CandidateJudgeError(Exception):
    """Fail-closed exception for candidate judge violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CandidateHardGateResult:
    """Outcome of a single deterministic hard validation gate on a candidate."""

    candidate_id: str
    gate_name: str
    passed: bool
    blocker: str | None = None
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id or not _IDENTIFIER_RE.match(self.candidate_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid candidate_id in hard gate result: {self.candidate_id!r}",
            )
        if not self.gate_name:
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                "Gate name must not be empty",
            )
        if not self.passed and not self.blocker:
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Failed hard gate {self.gate_name!r} must specify a blocker reason",
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateHardGateResult:
        return cls(
            candidate_id=data["candidate_id"],
            gate_name=data["gate_name"],
            passed=bool(data["passed"]),
            blocker=data.get("blocker"),
            evidence=str(data.get("evidence", "")),
        )


@dataclass(frozen=True)
class CandidateSemanticScore:
    """Normalized semantic score and rationale for an eligible candidate."""

    candidate_id: str
    score: float
    rationale: str
    evaluator_id: str

    def __post_init__(self) -> None:
        if not self.candidate_id or not _IDENTIFIER_RE.match(self.candidate_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        if math.isnan(self.score) or math.isinf(self.score):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value,
                f"Semantic score cannot be NaN or Infinite: {self.score}",
            )
        if not (0.0 <= self.score <= 1.0):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value,
                f"Semantic score must be in range [0.0, 1.0], got {self.score}",
            )
        if not self.evaluator_id:
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value,
                "evaluator_id must not be empty",
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateSemanticScore:
        return cls(
            candidate_id=data["candidate_id"],
            score=float(data["score"]),
            rationale=str(data.get("rationale", "")),
            evaluator_id=str(data.get("evaluator_id", "default")),
        )


@dataclass(frozen=True)
class CandidateJudgement:
    """Complete evaluation outcome for a single candidate."""

    candidate_id: str
    hard_gate_passed: bool
    eligible: bool
    semantic_score: CandidateSemanticScore | None = None
    rank: int | None = None
    blockers: tuple[str, ...] = ()
    rationale: str = ""
    hard_gate_results: tuple[CandidateHardGateResult, ...] = ()

    def __post_init__(self) -> None:
        if not self.candidate_id or not _IDENTIFIER_RE.match(self.candidate_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        # Core Invariant: hard validation > semantic review
        if not self.hard_gate_passed and self.eligible:
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_HARD_VALIDATION_FAILED.value,
                f"Candidate {self.candidate_id} cannot be eligible when hard gates failed",
            )
        if not self.hard_gate_passed and self.semantic_score is not None:
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_HARD_VALIDATION_FAILED.value,
                f"Candidate {self.candidate_id} must not have semantic score when hard gates failed",
            )
        if self.rank is not None and self.rank < 1:
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Rank must be >= 1, got {self.rank}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "hard_gate_passed": self.hard_gate_passed,
            "eligible": self.eligible,
            "semantic_score": self.semantic_score.to_dict() if self.semantic_score else None,
            "rank": self.rank,
            "blockers": list(self.blockers),
            "rationale": self.rationale,
            "hard_gate_results": [g.to_dict() for g in self.hard_gate_results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateJudgement:
        score_data = data.get("semantic_score")
        semantic_score = CandidateSemanticScore.from_dict(score_data) if score_data else None
        gate_results = tuple(CandidateHardGateResult.from_dict(g) for g in data.get("hard_gate_results", []))
        return cls(
            candidate_id=data["candidate_id"],
            hard_gate_passed=bool(data["hard_gate_passed"]),
            eligible=bool(data["eligible"]),
            semantic_score=semantic_score,
            rank=data.get("rank"),
            blockers=tuple(data.get("blockers", ())),
            rationale=str(data.get("rationale", "")),
            hard_gate_results=gate_results,
        )


@dataclass(frozen=True)
class CandidateJudgeRequest:
    """Request to judge a set of candidate results for a given task node."""

    judge_id: str
    task_id: str
    node_id: str
    base_sha: str
    candidates: tuple[CandidateResult, ...]
    required_hard_gates: tuple[str, ...] = (
        "CANDIDATE_STATE_COMPLETED",
        "BASE_SHA_CONSISTENCY",
        "WORKSPACE_IDENTITY_VALID",
        "RUN_IDENTITY_VALID",
        "CANDIDATE_IDENTITY_CONSISTENT",
        "NO_STALE_EXECUTION",
        "NO_SCOPE_VIOLATION",
        "NO_UNSAFE_MUTATION",
        "REQUIRED_VALIDATIONS_PASSED",
        "NO_INHERITED_BLOCKERS",
    )
    semantic_policy: str = "DEFAULT"
    allow_tie_break: bool = True

    def __post_init__(self) -> None:
        if not self.judge_id or not _IDENTIFIER_RE.match(self.judge_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid judge_id: {self.judge_id!r}",
            )
        if not self.task_id or not _IDENTIFIER_RE.match(self.task_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not self.node_id or not _IDENTIFIER_RE.match(self.node_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid node_id: {self.node_id!r}",
            )
        if not self.base_sha or not _HEX_40_RE.match(self.base_sha):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )

        # Check candidate duplicate IDs
        seen_ids: set[str] = set()
        for cand in self.candidates:
            if cand.candidate_id in seen_ids:
                raise CandidateJudgeError(
                    JudgeBlockingReason.CANDIDATE_ID_COLLISION.value,
                    f"Duplicate candidate_id in judge request: {cand.candidate_id!r}",
                )
            seen_ids.add(cand.candidate_id)

            # Check base SHA drift across candidate batch
            if cand.base_sha != self.base_sha:
                raise CandidateJudgeError(
                    JudgeBlockingReason.CANDIDATE_BASE_DRIFT.value,
                    f"Candidate {cand.candidate_id} base_sha {cand.base_sha} does not match request base_sha {self.base_sha}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_id": self.judge_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "candidates": [c.to_dict() for c in self.candidates],
            "required_hard_gates": list(self.required_hard_gates),
            "semantic_policy": self.semantic_policy,
            "allow_tie_break": self.allow_tie_break,
        }


@dataclass(frozen=True)
class CandidateJudgeResult:
    """Deterministic output of candidate judging."""

    judge_id: str
    task_id: str
    node_id: str
    base_sha: str
    judgements: tuple[CandidateJudgement, ...]
    selected_candidate_id: str | None
    decision_state: CandidateDecisionState
    completed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    rationale: str = ""
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.judge_id or not _IDENTIFIER_RE.match(self.judge_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid judge_id: {self.judge_id!r}",
            )
        if not self.task_id or not _IDENTIFIER_RE.match(self.task_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not self.node_id or not _IDENTIFIER_RE.match(self.node_id):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                f"Invalid node_id: {self.node_id!r}",
            )
        if not self.base_sha or not _HEX_40_RE.match(self.base_sha):
            raise CandidateJudgeError(
                JudgeBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )

        # Selected candidate must belong to eligible judgements
        if self.selected_candidate_id is not None:
            eligible_ids = {j.candidate_id for j in self.judgements if j.eligible}
            if self.selected_candidate_id not in eligible_ids:
                raise CandidateJudgeError(
                    JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value,
                    f"Selected candidate {self.selected_candidate_id!r} is not among eligible candidates: {eligible_ids}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_id": self.judge_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "judgements": [j.to_dict() for j in self.judgements],
            "selected_candidate_id": self.selected_candidate_id,
            "decision_state": self.decision_state.value,
            "completed_at": self.completed_at,
            "rationale": self.rationale,
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateJudgeResult:
        judgements = tuple(CandidateJudgement.from_dict(j) for j in data.get("judgements", []))
        return cls(
            judge_id=data["judge_id"],
            task_id=data["task_id"],
            node_id=data["node_id"],
            base_sha=data["base_sha"],
            judgements=judgements,
            selected_candidate_id=data.get("selected_candidate_id"),
            decision_state=CandidateDecisionState(data["decision_state"]),
            completed_at=data.get("completed_at", datetime.now(timezone.utc).isoformat()),
            rationale=str(data.get("rationale", "")),
            blockers=tuple(data.get("blockers", ())),
        )
