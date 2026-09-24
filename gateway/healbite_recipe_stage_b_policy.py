from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Sequence

from gateway.healbite_recipe_catalog_domain import TargetRightsScope

logger = logging.getLogger(__name__)

STAGE_B_MAX_ALLOWLIST_USERS: int = 5
STAGE_B_REQUIRED_RIGHTS_SCOPE: str = "FR"
STAGE_B_DEFAULT_PUBLIC: bool = False
MAX_PROVIDER_CALLS_PER_GENERATION: int = 3
DEFAULT_ACTOR_GENERATION_WINDOW_SECONDS: float = 3600.0
DEFAULT_MAX_GENERATIONS_PER_WINDOW: int = 10


def safe_pseudonym(identifier: object) -> str:
    """Return an 8-character hex digest of the identifier to avoid logging raw IDs."""
    if identifier is None:
        return "none"
    return hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class StageBPolicyDecision:
    status: str
    valid: bool
    allowlist_count: int = 0
    public_access: bool = False
    rights_scope: str | None = None
    error_message: str | None = None

    @property
    def ready(self) -> bool:
        return self.valid


def evaluate_stage_b_cohort_policy(
    allowlist: Collection[int] | str | None,
    *,
    public_access: bool = False,
    target_rights_scope: str | Sequence[str] | TargetRightsScope | None = None,
) -> StageBPolicyDecision:
    """
    Validate Stage B cohort configuration:
    - PUBLIC must remain False
    - allowlist count must be bounded between 1 and STAGE_B_MAX_ALLOWLIST_USERS (5)
    - target rights scope must be explicitly set to ('FR',)
    Fail-closed on any breach.
    """
    if public_access:
        return StageBPolicyDecision(
            status="public_not_allowed",
            valid=False,
            public_access=True,
            error_message="PUBLIC access is forbidden during Stage B canary",
        )

    parsed_allowlist: set[int] = set()
    if isinstance(allowlist, str):
        for part in allowlist.replace(";", ",").split(","):
            token = part.strip()
            if not token:
                continue
            if not token.isdigit():
                return StageBPolicyDecision(
                    status="invalid_allowlist_token",
                    valid=False,
                    error_message=f"Non-numeric token in allowlist: '{token}'",
                )
            parsed_allowlist.add(int(token))
    elif allowlist is not None:
        for item in allowlist:
            try:
                parsed_allowlist.add(int(item))
            except (ValueError, TypeError):
                return StageBPolicyDecision(
                    status="invalid_allowlist_item",
                    valid=False,
                    error_message=f"Non-numeric item in allowlist: '{item}'",
                )

    allowlist_count = len(parsed_allowlist)
    if allowlist_count < 1:
        return StageBPolicyDecision(
            status="allowlist_empty",
            valid=False,
            allowlist_count=0,
            error_message="Stage B allowlist cannot be empty; minimum 1 user required",
        )

    if allowlist_count > STAGE_B_MAX_ALLOWLIST_USERS:
        return StageBPolicyDecision(
            status="allowlist_exceeds_max",
            valid=False,
            allowlist_count=allowlist_count,
            error_message=(
                f"Stage B allowlist count ({allowlist_count}) exceeds "
                f"maximum allowed cohort limit ({STAGE_B_MAX_ALLOWLIST_USERS})"
            ),
        )

    # Validate rights scope
    if target_rights_scope is None:
        return StageBPolicyDecision(
            status="missing_rights_scope",
            valid=False,
            allowlist_count=allowlist_count,
            error_message="Target rights scope is missing; must be explicitly configured as 'FR'",
        )

    scope_obj = (
        target_rights_scope
        if isinstance(target_rights_scope, TargetRightsScope)
        else TargetRightsScope.from_value(target_rights_scope)
    )
    if scope_obj is None or scope_obj.jurisdictions != (STAGE_B_REQUIRED_RIGHTS_SCOPE,):
        configured_jur = scope_obj.jurisdictions if scope_obj else "none"
        return StageBPolicyDecision(
            status="invalid_rights_scope",
            valid=False,
            allowlist_count=allowlist_count,
            rights_scope=str(configured_jur),
            error_message=(
                f"Target rights scope '{configured_jur}' is not authorized for Stage B; "
                f"must strictly match ('{STAGE_B_REQUIRED_RIGHTS_SCOPE}',)"
            ),
        )

    return StageBPolicyDecision(
        status="ready",
        valid=True,
        allowlist_count=allowlist_count,
        public_access=False,
        rights_scope=STAGE_B_REQUIRED_RIGHTS_SCOPE,
    )


class GuardAcquireState:
    ACQUIRED = "acquired"
    REPLAY_READY = "replay_ready"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class GuardAcquireResult:
    state: str
    error: str | None = None


class RecipeGenerationGuard:
    """
    In-flight generation concurrency control and rate limiter for Recipe Grounded Menu.
    Guarantees:
    - at most 1 active generation per actor
    - at most 1 active generation per household
    - duplicate/replay requests for the same idempotency key wait for the in-flight generation
      rather than amplifying provider LLM calls
    - bounded completed generations per actor within a sliding time window
    """

    def __init__(
        self,
        *,
        max_per_window: int = DEFAULT_MAX_GENERATIONS_PER_WINDOW,
        window_seconds: float = DEFAULT_ACTOR_GENERATION_WINDOW_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._active_actors: set[int] = set()
        self._active_households: set[str] = set()
        self._active_keys: dict[str, threading.Event] = {}
        self._recent_generations: dict[int, list[float]] = {}
        self._max_per_window = max_per_window
        self._window_seconds = window_seconds

    def acquire(
        self,
        actor_user_id: int,
        household_id: str,
        *,
        idempotency_key: str | None = None,
        wait_timeout: float = 15.0,
    ) -> GuardAcquireResult:
        with self._lock:
            # 1. If exact same idempotency_key is already in flight, wait for it
            if idempotency_key is not None and idempotency_key in self._active_keys:
                event = self._active_keys[idempotency_key]
                # Release lock while waiting for the in-flight task to complete
                self._lock.release()
                try:
                    finished = event.wait(timeout=wait_timeout)
                    if finished:
                        return GuardAcquireResult(state=GuardAcquireState.REPLAY_READY)
                    return GuardAcquireResult(
                        state=GuardAcquireState.CONFLICT,
                        error="Concurrent generation in flight for this idempotency key timed out",
                    )
                finally:
                    self._lock.acquire()

            # 2. Check sliding window rate limit
            now = time.monotonic()
            timestamps = self._recent_generations.get(actor_user_id, [])
            valid_timestamps = [t for t in timestamps if (now - t) < self._window_seconds]
            self._recent_generations[actor_user_id] = valid_timestamps
            if len(valid_timestamps) >= self._max_per_window:
                return GuardAcquireResult(
                    state=GuardAcquireState.RATE_LIMITED,
                    error=f"Actor recipe generation rate limit exceeded ({self._max_per_window}/window)",
                )

            # 3. Check single active generation per actor
            if actor_user_id in self._active_actors:
                return GuardAcquireResult(
                    state=GuardAcquireState.CONFLICT,
                    error="Active recipe generation already in progress for this actor",
                )

            # 4. Check single active generation per household
            if household_id in self._active_households:
                return GuardAcquireResult(
                    state=GuardAcquireState.CONFLICT,
                    error="Active recipe generation already in progress for this household",
                )

            # 5. Acquire lease
            self._active_actors.add(actor_user_id)
            self._active_households.add(household_id)
            if idempotency_key is not None:
                self._active_keys[idempotency_key] = threading.Event()

            return GuardAcquireResult(state=GuardAcquireState.ACQUIRED)

    def release(
        self,
        actor_user_id: int,
        household_id: str,
        *,
        idempotency_key: str | None = None,
        success: bool = True,
    ) -> None:
        with self._lock:
            self._active_actors.discard(actor_user_id)
            self._active_households.discard(household_id)
            if idempotency_key is not None and idempotency_key in self._active_keys:
                event = self._active_keys.pop(idempotency_key)
                event.set()
            if success:
                self._recent_generations.setdefault(actor_user_id, []).append(time.monotonic())

    def reset(self) -> None:
        """Reset internal state (for testing)."""
        with self._lock:
            for event in self._active_keys.values():
                event.set()
            self._active_actors.clear()
            self._active_households.clear()
            self._active_keys.clear()
            self._recent_generations.clear()


# Default singleton guard instance for service runtime
_GLOBAL_RECIPE_GENERATION_GUARD = RecipeGenerationGuard()


def get_default_generation_guard() -> RecipeGenerationGuard:
    return _GLOBAL_RECIPE_GENERATION_GUARD
