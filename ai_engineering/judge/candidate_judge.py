"""Deterministic Candidate Judge implementation."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Callable

from ai_engineering.candidates.candidate_contracts import (
    CandidateBlockingReason,
    CandidateResult,
    CandidateState,
)
from ai_engineering.judge.judge_contracts import (
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
from ai_engineering.judge.semantic_evaluator import SemanticCandidateEvaluator

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class CandidateJudge:
    """Deterministic candidate evaluation and selection judge."""

    def __init__(
        self,
        semantic_evaluator: SemanticCandidateEvaluator | None = None,
        event_sink: Callable[[JudgeEventType, dict[str, str]], None] | None = None,
    ) -> None:
        self.semantic_evaluator = semantic_evaluator
        self.event_sink = event_sink

    def _emit(self, event_type: JudgeEventType, details: dict[str, str]) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, details)

    def evaluate_hard_gates(
        self,
        candidate: CandidateResult,
        request: CandidateJudgeRequest,
    ) -> tuple[bool, tuple[CandidateHardGateResult, ...], tuple[str, ...]]:
        """Run mandatory deterministic hard validation gates on a single candidate."""
        results: list[CandidateHardGateResult] = []
        blockers: list[str] = []

        # Gate 1: CANDIDATE_STATE_COMPLETED
        if candidate.state == CandidateState.COMPLETED and candidate.success:
            results.append(CandidateHardGateResult(candidate.candidate_id, "CANDIDATE_STATE_COMPLETED", True))
        else:
            blocker = CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value
            results.append(CandidateHardGateResult(candidate.candidate_id, "CANDIDATE_STATE_COMPLETED", False, blocker=blocker, evidence=f"State: {candidate.state.value}, Success: {candidate.success}"))
            blockers.append(blocker)

        # Gate 2: BASE_SHA_CONSISTENCY
        if candidate.base_sha == request.base_sha:
            results.append(CandidateHardGateResult(candidate.candidate_id, "BASE_SHA_CONSISTENCY", True))
        else:
            blocker = JudgeBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value
            results.append(CandidateHardGateResult(candidate.candidate_id, "BASE_SHA_CONSISTENCY", False, blocker=blocker, evidence=f"Base SHA: {candidate.base_sha} != Request: {request.base_sha}"))
            blockers.append(blocker)

        # Gate 3: WORKSPACE_IDENTITY_VALID
        if candidate.workspace_id and _IDENTIFIER_RE.match(candidate.workspace_id):
            results.append(CandidateHardGateResult(candidate.candidate_id, "WORKSPACE_IDENTITY_VALID", True))
        else:
            blocker = CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value
            results.append(CandidateHardGateResult(candidate.candidate_id, "WORKSPACE_IDENTITY_VALID", False, blocker=blocker, evidence=f"Invalid workspace_id: {candidate.workspace_id!r}"))
            blockers.append(blocker)

        # Gate 4: RUN_IDENTITY_VALID
        if candidate.run_id and _IDENTIFIER_RE.match(candidate.run_id):
            results.append(CandidateHardGateResult(candidate.candidate_id, "RUN_IDENTITY_VALID", True))
        else:
            blocker = CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value
            results.append(CandidateHardGateResult(candidate.candidate_id, "RUN_IDENTITY_VALID", False, blocker=blocker, evidence=f"Invalid run_id: {candidate.run_id!r}"))
            blockers.append(blocker)

        # Gate 5: CANDIDATE_IDENTITY_CONSISTENT
        if candidate.task_id == request.task_id and candidate.node_id == request.node_id:
            results.append(CandidateHardGateResult(candidate.candidate_id, "CANDIDATE_IDENTITY_CONSISTENT", True))
        else:
            blocker = JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value
            results.append(CandidateHardGateResult(candidate.candidate_id, "CANDIDATE_IDENTITY_CONSISTENT", False, blocker=blocker, evidence=f"task/node mismatch: ({candidate.task_id}, {candidate.node_id}) vs ({request.task_id}, {request.node_id})"))
            blockers.append(blocker)

        # Gate 6: NO_STALE_EXECUTION
        stale_blockers = {
            JudgeBlockingReason.STALE_RUN_EVENT.value,
            JudgeBlockingReason.STALE_RUN_MUTATION.value,
            JudgeBlockingReason.RUN_WORKSPACE_MISMATCH.value,
        }
        found_stale = [b for b in candidate.blockers if b in stale_blockers]
        if not found_stale:
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_STALE_EXECUTION", True))
        else:
            blocker = found_stale[0]
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_STALE_EXECUTION", False, blocker=blocker, evidence=f"Stale execution blockers: {found_stale}"))
            blockers.append(blocker)

        # Gate 7: NO_SCOPE_VIOLATION
        scope_blockers = {
            JudgeBlockingReason.CANDIDATE_SCOPE_VIOLATION.value,
            JudgeBlockingReason.CANDIDATE_PATH_ESCAPE.value,
        }
        found_scope = [b for b in candidate.blockers if b in scope_blockers]
        if not found_scope:
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_SCOPE_VIOLATION", True))
        else:
            blocker = found_scope[0]
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_SCOPE_VIOLATION", False, blocker=blocker, evidence=f"Scope violation blockers: {found_scope}"))
            blockers.append(blocker)

        # Gate 8: NO_UNSAFE_MUTATION
        unsafe_blockers = {
            JudgeBlockingReason.CANDIDATE_MAIN_WORKTREE_FORBIDDEN.value,
            JudgeBlockingReason.CANDIDATE_NOT_AUTHORIZED.value,
        }
        found_unsafe = [b for b in candidate.blockers if b in unsafe_blockers]
        if not found_unsafe:
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_UNSAFE_MUTATION", True))
        else:
            blocker = found_unsafe[0]
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_UNSAFE_MUTATION", False, blocker=blocker, evidence=f"Unsafe mutation blockers: {found_unsafe}"))
            blockers.append(blocker)

        # Gate 9: REQUIRED_VALIDATIONS_PASSED
        failed_validations = [v for v in candidate.validation_results if not v.success]
        if not failed_validations:
            results.append(CandidateHardGateResult(candidate.candidate_id, "REQUIRED_VALIDATIONS_PASSED", True))
        else:
            blocker = JudgeBlockingReason.CANDIDATE_HARD_VALIDATION_FAILED.value
            failed_cmds = [v.command for v in failed_validations]
            results.append(CandidateHardGateResult(candidate.candidate_id, "REQUIRED_VALIDATIONS_PASSED", False, blocker=blocker, evidence=f"Failed validation commands: {failed_cmds}"))
            blockers.append(blocker)

        # Gate 10: NO_INHERITED_BLOCKERS
        inherited_unhandled = [b for b in candidate.blockers if b not in blockers]
        if not inherited_unhandled:
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_INHERITED_BLOCKERS", True))
        else:
            blocker = inherited_unhandled[0]
            results.append(CandidateHardGateResult(candidate.candidate_id, "NO_INHERITED_BLOCKERS", False, blocker=blocker, evidence=f"Inherited blockers: {inherited_unhandled}"))
            blockers.extend(inherited_unhandled)

        all_passed = len(blockers) == 0
        return all_passed, tuple(results), tuple(blockers)

    def judge(self, request: CandidateJudgeRequest) -> CandidateJudgeResult:
        """Execute deterministic candidate judging for a batch of candidate results."""
        self._emit(JudgeEventType.CANDIDATE_JUDGE_STARTED, {"judge_id": request.judge_id, "task_id": request.task_id})

        if not request.candidates:
            self._emit(JudgeEventType.CANDIDATE_JUDGE_NO_ELIGIBLE, {"reason": "NO_CANDIDATES"})
            return CandidateJudgeResult(
                judge_id=request.judge_id,
                task_id=request.task_id,
                node_id=request.node_id,
                base_sha=request.base_sha,
                judgements=(),
                selected_candidate_id=None,
                decision_state=CandidateDecisionState.NO_CANDIDATES,
                rationale="No candidates provided in judge request",
            )

        # Sort candidates deterministically by candidate_id for input-order independence
        sorted_candidates = sorted(request.candidates, key=lambda c: c.candidate_id)

        judgements_by_id: dict[str, CandidateJudgement] = {}
        eligible_candidates: list[CandidateResult] = []

        # Step 1: Execute Hard Gate Pipeline
        for candidate in sorted_candidates:
            hard_passed, gate_results, blockers = self.evaluate_hard_gates(candidate, request)
            if hard_passed:
                self._emit(JudgeEventType.CANDIDATE_HARD_GATE_PASSED, {"candidate_id": candidate.candidate_id})
                eligible_candidates.append(candidate)
                # Tentatively eligible, semantic review will be populated next
                judgements_by_id[candidate.candidate_id] = CandidateJudgement(
                    candidate_id=candidate.candidate_id,
                    hard_gate_passed=True,
                    eligible=True,
                    hard_gate_results=gate_results,
                )
            else:
                self._emit(JudgeEventType.CANDIDATE_HARD_GATE_FAILED, {"candidate_id": candidate.candidate_id, "blockers": ",".join(blockers)})
                # Ineligible: SEMANTIC EVALUATOR WILL NOT BE CALLED
                judgements_by_id[candidate.candidate_id] = CandidateJudgement(
                    candidate_id=candidate.candidate_id,
                    hard_gate_passed=False,
                    eligible=False,
                    semantic_score=None,
                    rank=None,
                    blockers=blockers,
                    rationale="Failed mandatory hard validation gates",
                    hard_gate_results=gate_results,
                )

        # Step 2: Handle Zero Eligible Candidates
        if not eligible_candidates:
            self._emit(JudgeEventType.CANDIDATE_JUDGE_NO_ELIGIBLE, {"reason": "NO_ELIGIBLE_CANDIDATES"})
            # Preserve deterministic sorted judgements
            judgements = tuple(judgements_by_id[c.candidate_id] for c in sorted_candidates)
            return CandidateJudgeResult(
                judge_id=request.judge_id,
                task_id=request.task_id,
                node_id=request.node_id,
                base_sha=request.base_sha,
                judgements=judgements,
                selected_candidate_id=None,
                decision_state=CandidateDecisionState.NO_ELIGIBLE_CANDIDATES,
                rationale="All candidates failed mandatory hard validation gates",
                blockers=(JudgeBlockingReason.CANDIDATE_JUDGE_NO_ELIGIBLE.value,),
            )

        # Step 3: Run Semantic Review on Eligible Candidates Only
        scored_eligible: list[tuple[CandidateResult, CandidateSemanticScore]] = []
        for candidate in eligible_candidates:
            self._emit(JudgeEventType.CANDIDATE_SEMANTIC_REVIEW_STARTED, {"candidate_id": candidate.candidate_id})
            if self.semantic_evaluator is not None:
                try:
                    score = self.semantic_evaluator.evaluate(candidate)
                except Exception as exc:
                    self._emit(JudgeEventType.CANDIDATE_JUDGE_FAILED, {"error": str(exc), "candidate_id": candidate.candidate_id})
                    judgements = tuple(judgements_by_id[c.candidate_id] for c in sorted_candidates)
                    return CandidateJudgeResult(
                        judge_id=request.judge_id,
                        task_id=request.task_id,
                        node_id=request.node_id,
                        base_sha=request.base_sha,
                        judgements=judgements,
                        selected_candidate_id=None,
                        decision_state=CandidateDecisionState.JUDGE_FAILED,
                        rationale=f"Semantic evaluator raised an error on candidate {candidate.candidate_id}: {exc}",
                        blockers=(JudgeBlockingReason.CANDIDATE_SEMANTIC_EVALUATION_FAILED.value,),
                    )

                # Validate score object
                if not isinstance(score, CandidateSemanticScore) or score.candidate_id != candidate.candidate_id:
                    self._emit(JudgeEventType.CANDIDATE_JUDGE_FAILED, {"error": "Invalid score object", "candidate_id": candidate.candidate_id})
                    judgements = tuple(judgements_by_id[c.candidate_id] for c in sorted_candidates)
                    return CandidateJudgeResult(
                        judge_id=request.judge_id,
                        task_id=request.task_id,
                        node_id=request.node_id,
                        base_sha=request.base_sha,
                        judgements=judgements,
                        selected_candidate_id=None,
                        decision_state=CandidateDecisionState.JUDGE_FAILED,
                        rationale=f"Semantic evaluator returned invalid score for {candidate.candidate_id}",
                        blockers=(JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value,),
                    )
            else:
                # Default baseline score if evaluator omitted
                score = CandidateSemanticScore(
                    candidate_id=candidate.candidate_id,
                    score=1.0,
                    rationale="Default eligible pass score",
                    evaluator_id="default-pass",
                )

            self._emit(JudgeEventType.CANDIDATE_SEMANTIC_REVIEW_COMPLETED, {"candidate_id": candidate.candidate_id, "score": str(score.score)})
            scored_eligible.append((candidate, score))

        # Step 4: Rank Eligible Candidates
        # Sort by (-score, candidate_id) for deterministic ordering
        scored_eligible.sort(key=lambda item: (-item[1].score, item[0].candidate_id))

        # Check for top score tie
        has_tie = False
        if len(scored_eligible) > 1 and math.isclose(scored_eligible[0][1].score, scored_eligible[1][1].score, abs_tol=1e-9):
            has_tie = True

        # Assign ranks
        for rank_idx, (cand, score) in enumerate(scored_eligible, start=1):
            prior_j = judgements_by_id[cand.candidate_id]
            judgements_by_id[cand.candidate_id] = CandidateJudgement(
                candidate_id=cand.candidate_id,
                hard_gate_passed=True,
                eligible=True,
                semantic_score=score,
                rank=rank_idx,
                blockers=(),
                rationale=f"Rank {rank_idx} with semantic score {score.score:.2f}",
                hard_gate_results=prior_j.hard_gate_results,
            )

        # Assemble final judgements tuple in stable order
        final_judgements = tuple(judgements_by_id[c.candidate_id] for c in sorted_candidates)

        # Step 5: Decision State and Selection
        if len(scored_eligible) == 1:
            winner_id = scored_eligible[0][0].candidate_id
            self._emit(JudgeEventType.CANDIDATE_SELECTED, {"candidate_id": winner_id, "decision_state": CandidateDecisionState.SINGLE_ELIGIBLE.value})
            return CandidateJudgeResult(
                judge_id=request.judge_id,
                task_id=request.task_id,
                node_id=request.node_id,
                base_sha=request.base_sha,
                judgements=final_judgements,
                selected_candidate_id=winner_id,
                decision_state=CandidateDecisionState.SINGLE_ELIGIBLE,
                rationale=f"Single eligible candidate {winner_id} selected",
            )

        # Multiple eligible candidates
        if has_tie and not request.allow_tie_break:
            self._emit(JudgeEventType.CANDIDATE_JUDGE_FAILED, {"reason": "TIE"})
            return CandidateJudgeResult(
                judge_id=request.judge_id,
                task_id=request.task_id,
                node_id=request.node_id,
                base_sha=request.base_sha,
                judgements=final_judgements,
                selected_candidate_id=None,
                decision_state=CandidateDecisionState.TIE,
                rationale="Top candidates tied in semantic score and tie-breaking disallowed",
                blockers=(JudgeBlockingReason.CANDIDATE_JUDGE_TIE.value,),
            )

        winner_id = scored_eligible[0][0].candidate_id
        tie_break_note = " (resolved via lexical tie-break)" if has_tie else ""
        self._emit(JudgeEventType.CANDIDATE_SELECTED, {"candidate_id": winner_id, "decision_state": CandidateDecisionState.RANKED_SELECTION.value})
        return CandidateJudgeResult(
            judge_id=request.judge_id,
            task_id=request.task_id,
            node_id=request.node_id,
            base_sha=request.base_sha,
            judgements=final_judgements,
            selected_candidate_id=winner_id,
            decision_state=CandidateDecisionState.RANKED_SELECTION,
            rationale=f"Rank 1 candidate {winner_id} selected with score {scored_eligible[0][1].score:.2f}{tie_break_note}",
        )
