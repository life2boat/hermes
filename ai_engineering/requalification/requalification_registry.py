"""In-memory thread-safe registry for CandidateRequalificationResult records."""

from __future__ import annotations

import threading

from ai_engineering.requalification.requalification_contracts import (
    CandidateRequalificationResult,
    RequalificationBlockingReason,
    RequalificationError,
)


class RequalificationRegistry:
    """In-memory domain registry for immutable candidate requalification outcomes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, CandidateRequalificationResult] = {}
        self._by_candidate: dict[str, list[CandidateRequalificationResult]] = {}

    def record(self, result: CandidateRequalificationResult) -> None:
        """Record an immutable CandidateRequalificationResult with idempotency and collision checks."""
        if not isinstance(result, CandidateRequalificationResult):
            raise RequalificationError(
                RequalificationBlockingReason.CANDIDATE_RESULT_INVALID.value,
                "Expected CandidateRequalificationResult instance",
            )

        with self._lock:
            # Idempotency / Collision check
            if result.requalification_id in self._results:
                existing = self._results[result.requalification_id]
                if existing == result:
                    return  # Exact idempotent registration
                raise RequalificationError(
                    RequalificationBlockingReason.REQUALIFICATION_COLLISION.value,
                    f"Requalification ID collision with divergent content: {result.requalification_id}",
                )

            self._results[result.requalification_id] = result
            if result.candidate_id not in self._by_candidate:
                self._by_candidate[result.candidate_id] = []
            self._by_candidate[result.candidate_id].append(result)

    def get(self, requalification_id: str) -> CandidateRequalificationResult | None:
        """Retrieve requalification result by ID."""
        with self._lock:
            return self._results.get(requalification_id)

    def latest_for_candidate(self, candidate_id: str) -> CandidateRequalificationResult | None:
        """Retrieve the latest requalification result recorded for a candidate."""
        with self._lock:
            history = self._by_candidate.get(candidate_id, [])
            return history[-1] if history else None

    def list_for_candidate(self, candidate_id: str) -> tuple[CandidateRequalificationResult, ...]:
        """List all requalification results for a candidate in deterministic order."""
        with self._lock:
            history = self._by_candidate.get(candidate_id, [])
            return tuple(history)
