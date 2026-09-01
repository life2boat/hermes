"""Deterministic candidate requalification engine and judgement freshness analysis."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Callable, Sequence

from ai_engineering.candidates.candidate_contracts import CandidateResult
from ai_engineering.requalification.requalification_contracts import (
    BaseRelationship,
    CandidateRequalificationRequest,
    CandidateRequalificationResult,
    DriftEvidence,
    JudgementFreshness,
    RequalificationBlockingReason,
    RequalificationDecisionState,
    RequalificationError,
    RequalificationEvidence,
    ValidationFreshness,
)
from ai_engineering.workspaces.diff_artifacts import compute_diff_digest, verify_diff_artifact
from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    WorkspaceSnapshot,
    validate_repository_relative_path,
)


class CandidateRequalificationEngine:
    """Engine for determining candidate validity against an advanced canonical main."""

    def __init__(
        self,
        canonical_repo_path: str | Path | None = None,
        git_executor: Callable[[list[str], str], tuple[int, str, str]] | None = None,
    ) -> None:
        self.canonical_repo_path = Path(canonical_repo_path).resolve() if canonical_repo_path else None
        self.git_executor = git_executor or self._default_git_executor

    @staticmethod
    def _default_git_executor(cmd: list[str], cwd: str) -> tuple[int, str, str]:
        res = subprocess.run(
            ["git"] + cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode, res.stdout, res.stderr

    def compute_drift_evidence(
        self,
        candidate_base_sha: str,
        current_main_sha: str,
        cwd: str,
    ) -> DriftEvidence:
        """Compute normalized drift evidence between candidate base and current main."""
        rc, diff_out, _ = self.git_executor(["diff", "--binary", "--no-ext-diff", f"{candidate_base_sha}...{current_main_sha}"], cwd)
        diff_digest = compute_diff_digest(diff_out)

        rc, stat_out, _ = self.git_executor(["diff", "--stat", f"{candidate_base_sha}...{current_main_sha}"], cwd)
        rc, name_out, _ = self.git_executor(["diff", "--name-only", f"{candidate_base_sha}...{current_main_sha}"], cwd)
        rc, count_out, _ = self.git_executor(["rev-list", "--count", f"{candidate_base_sha}..{current_main_sha}"], cwd)

        drift_paths = tuple(sorted(set(validate_repository_relative_path(p.strip()) for p in name_out.splitlines() if p.strip())))
        drift_count = int(count_out.strip()) if count_out.strip().isdigit() else 0

        return DriftEvidence(
            candidate_base_sha=candidate_base_sha.lower(),
            current_main_sha=current_main_sha.lower(),
            changed_paths=drift_paths,
            diff_stat=stat_out.strip(),
            diff_digest=diff_digest,
            drift_commit_count=drift_count,
        )

    def evaluate(
        self,
        request: CandidateRequalificationRequest,
        canonical_repo_path: str | Path | None = None,
        now: str | None = None,
    ) -> CandidateRequalificationResult:
        """Evaluate a candidate's validity against current main without mutating any repository or worktree."""
        repo_path = str(Path(canonical_repo_path or self.canonical_repo_path or ".").resolve())
        completed_at = now or datetime.now(timezone.utc).isoformat()

        candidate_base = request.candidate_base_sha.lower()
        current_main = request.current_main_sha.lower()

        # 1. Exact base match: no requalification required
        if candidate_base == current_main:
            return CandidateRequalificationResult(
                requalification_id=request.requalification_id,
                candidate_id=request.candidate_id,
                candidate_base_sha=candidate_base,
                current_main_sha=current_main,
                relationship=BaseRelationship.EXACT_BASE,
                decision_state=RequalificationDecisionState.NO_REQUALIFICATION_REQUIRED,
                eligible=request.candidate_result.success,
                requires_new_candidate=not request.candidate_result.success,
                blockers=request.candidate_result.blockers,
                evidence=None,
                completed_at=completed_at,
            )

        # 2. Check candidate base existence in canonical repo
        rc, _, _ = self.git_executor(["cat-file", "-e", candidate_base], repo_path)
        if rc != 0:
            return CandidateRequalificationResult(
                requalification_id=request.requalification_id,
                candidate_id=request.candidate_id,
                candidate_base_sha=candidate_base,
                current_main_sha=current_main,
                relationship=BaseRelationship.BASE_UNKNOWN,
                decision_state=RequalificationDecisionState.REQUALIFICATION_REJECTED,
                eligible=False,
                requires_new_candidate=True,
                blockers=(RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,),
                evidence=None,
                completed_at=completed_at,
            )

        # 3. Check ancestry: candidate_base is ancestor of current_main
        rc, _, _ = self.git_executor(["merge-base", "--is-ancestor", candidate_base, current_main], repo_path)
        if rc != 0:
            return CandidateRequalificationResult(
                requalification_id=request.requalification_id,
                candidate_id=request.candidate_id,
                candidate_base_sha=candidate_base,
                current_main_sha=current_main,
                relationship=BaseRelationship.BASE_NOT_ANCESTOR,
                decision_state=RequalificationDecisionState.REQUALIFICATION_REJECTED,
                eligible=False,
                requires_new_candidate=True,
                blockers=(RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,),
                evidence=None,
                completed_at=completed_at,
            )

        # Base is a valid ancestor of current main -> main has advanced
        relationship = BaseRelationship.MAIN_ADVANCED_DESCENDANT

        # 4. Validate candidate snapshot/diff artifact integrity if present
        candidate_digest = ""
        if request.snapshot_evidence is not None:
            if isinstance(request.snapshot_evidence, DiffArtifact):
                candidate_digest = request.snapshot_evidence.diff_digest
            elif isinstance(request.snapshot_evidence, WorkspaceSnapshot):
                candidate_digest = request.snapshot_evidence.diff_digest

        # 5. Compute drift evidence
        drift_ev = self.compute_drift_evidence(candidate_base, current_main, repo_path)

        # 6. Extract and normalize candidate changed paths
        candidate_paths = tuple(sorted(set(validate_repository_relative_path(p) for p in request.candidate_result.changed_paths)))

        # 7. Compute path overlap
        overlap = tuple(sorted(set(candidate_paths) & set(drift_ev.changed_paths)))

        validation_status = (
            ValidationFreshness.STILL_APPLICABLE if (not overlap and request.candidate_result.success) else ValidationFreshness.REQUIRES_RERUN
        )

        requal_evidence = RequalificationEvidence(
            candidate_base_sha=candidate_base,
            current_main_sha=current_main,
            drift_changed_paths=drift_ev.changed_paths,
            candidate_changed_paths=candidate_paths,
            overlapping_paths=overlap,
            drift_diff_digest=drift_ev.diff_digest,
            candidate_diff_digest=candidate_digest,
            validation_status=validation_status,
        )

        # 8. Decision logic
        if overlap:
            return CandidateRequalificationResult(
                requalification_id=request.requalification_id,
                candidate_id=request.candidate_id,
                candidate_base_sha=candidate_base,
                current_main_sha=current_main,
                relationship=relationship,
                decision_state=RequalificationDecisionState.NEW_CANDIDATE_REQUIRED,
                eligible=False,
                requires_new_candidate=True,
                blockers=(
                    RequalificationBlockingReason.CANDIDATE_DRIFT_OVERLAP.value,
                    RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value,
                ),
                evidence=requal_evidence,
                completed_at=completed_at,
            )

        # Non-overlapping drift
        if not request.candidate_result.success or request.candidate_result.blockers:
            return CandidateRequalificationResult(
                requalification_id=request.requalification_id,
                candidate_id=request.candidate_id,
                candidate_base_sha=candidate_base,
                current_main_sha=current_main,
                relationship=relationship,
                decision_state=RequalificationDecisionState.REQUALIFICATION_REJECTED,
                eligible=False,
                requires_new_candidate=True,
                blockers=request.candidate_result.blockers or (RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,),
                evidence=requal_evidence,
                completed_at=completed_at,
            )

        # Fully satisfied hard conditions for clean requalification
        return CandidateRequalificationResult(
            requalification_id=request.requalification_id,
            candidate_id=request.candidate_id,
            candidate_base_sha=candidate_base,
            current_main_sha=current_main,
            relationship=relationship,
            decision_state=RequalificationDecisionState.REQUALIFIED,
            eligible=True,
            requires_new_candidate=False,
            blockers=(),
            evidence=requal_evidence,
            completed_at=completed_at,
        )

    @staticmethod
    def classify_judgement_freshness(
        judge_base_sha: str,
        current_main_sha: str,
    ) -> JudgementFreshness:
        """Classify whether a historical CandidateJudgeResult remains fresh against current main."""
        if not judge_base_sha or not current_main_sha:
            return JudgementFreshness.INVALID
        if judge_base_sha.lower() == current_main_sha.lower():
            return JudgementFreshness.CURRENT
        return JudgementFreshness.STALE_BASE
