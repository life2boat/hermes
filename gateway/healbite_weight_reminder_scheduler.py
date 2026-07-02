from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable

from gateway.healbite_weight_reminders import (
    DEFAULT_BASE_BACKOFF_SECONDS,
    DEFAULT_CLAIM_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    ReminderClaimOutcome,
    ReminderDeliveryState,
    ReminderDeliveryStatus,
    WeightReminderClaim,
    WeightReminderConfig,
    WeightReminderSetting,
    HealBiteWeightReminderStore,
    load_weight_reminder_config,
)

logger = logging.getLogger(__name__)

WEIGHT_REMINDER_TEXT = (
    "\u041f\u043e\u0440\u0430 \u0437\u0430\u043f\u0438\u0441\u0430\u0442\u044c \u0432\u0435\u0441"
    "\n\n"
    "\u0420\u0435\u0433\u0443\u043b\u044f\u0440\u043d\u044b\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043f\u043e\u043c\u043e\u0433\u0430\u044e\u0442 \u043e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u0442\u044c \u0434\u0438\u043d\u0430\u043c\u0438\u043a\u0443."
)


class WeightReminderFailureKind(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AMBIGUOUS = "ambiguous"
    PERMANENT = "permanent"
    GLOBAL_AUTH = "global_auth"


class WeightReminderDeliveryError(Exception):
    def __init__(
        self,
        kind: WeightReminderFailureKind,
        *,
        error_type: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(error_type)
        self.kind = kind
        self.error_type = _safe_error_type(error_type)
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class WeightReminderSchedulerStats:
    scanned: int = 0
    claimed: int = 0
    sent: int = 0
    skipped: int = 0
    retry_scheduled: int = 0
    permanent_failed: int = 0
    ambiguous: int = 0
    circuit_open: bool = False


def _safe_error_type(value: str | None) -> str:
    raw = (value or "unknown").strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-", ":"} else "_" for ch in raw)
    return cleaned[:64] or "unknown"


def _attempt_bucket(attempt_count: int) -> str:
    if attempt_count <= 1:
        return "1"
    if attempt_count == 2:
        return "2"
    return "3+"


def _timezone_region_bucket(timezone_name: str) -> str:
    if "/" in timezone_name:
        return timezone_name.split("/", 1)[0].lower()[:24]
    return "single"


def _time_bucket(local_time: str) -> str:
    try:
        hour = int(local_time.split(":", 1)[0])
    except Exception:
        return "unknown"
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 5:
        return "2-5"
    if value <= 20:
        return "6-20"
    return "21+"


def _log_marker(marker: str, **fields: object) -> None:
    allowed = {
        "route",
        "action",
        "outcome",
        "enabled",
        "delivery_state",
        "delivery_attempted",
        "delivery_completed",
        "retry_scheduled",
        "permanent_failure",
        "ambiguous_delivery",
        "claim_recovered",
        "stale_schedule",
        "timezone_present",
        "timezone_region_bucket",
        "weekday_bucket",
        "time_bucket",
        "attempt_count_bucket",
        "batch_size_bucket",
        "due_count_bucket",
        "error_type",
        "corr_present",
        "corr",
    }
    try:
        parts = [f"{key}={fields[key]}" for key in sorted(fields) if key in allowed]
        logger.info("[HealBite][%s] %s", marker, " ".join(parts))
    except Exception:
        # Observability must never affect reminder delivery or scheduler health.
        logger.debug("weight reminder marker emission failed", exc_info=False)


def classify_send_exception(exc: Exception) -> WeightReminderDeliveryError:
    if isinstance(exc, WeightReminderDeliveryError):
        return exc
    name = exc.__class__.__name__.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    retry_after = getattr(exc, "retry_after", None)
    if status == 429 or "retryafter" in name or "ratelimit" in name:
        return WeightReminderDeliveryError(
            WeightReminderFailureKind.RATE_LIMIT,
            error_type="rate_limited",
            retry_after_seconds=int(retry_after) if retry_after else None,
        )
    recipient_forbidden = (
        "blocked" in name
        or "chatnotfound" in name
        or ("forbidden" in name and "auth" not in name and "token" not in name and "unauthorized" not in name)
    )
    if recipient_forbidden:
        return WeightReminderDeliveryError(WeightReminderFailureKind.PERMANENT, error_type="recipient_unavailable")
    if status == 401 or (status == 403 and ("auth" in name or "token" in name or "unauthorized" in name)):
        return WeightReminderDeliveryError(WeightReminderFailureKind.GLOBAL_AUTH, error_type="bot_auth_failed")
    if "timeout" in name or "timedout" in name:
        return WeightReminderDeliveryError(WeightReminderFailureKind.AMBIGUOUS, error_type="send_timeout")
    if "network" in name or "connection" in name:
        return WeightReminderDeliveryError(WeightReminderFailureKind.TRANSIENT, error_type="network_error")
    return WeightReminderDeliveryError(WeightReminderFailureKind.AMBIGUOUS, error_type="unknown_send_result")


class WeightReminderScheduler:
    def __init__(
        self,
        *,
        store: HealBiteWeightReminderStore,
        config: WeightReminderConfig,
        send_reminder: Callable[[int, str], Awaitable[None]],
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.send_reminder = send_reminder
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.sleep_fn = sleep_fn or asyncio.sleep
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._circuit_open = False

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def should_start(self) -> bool:
        return bool(self.config.enabled and self.config.allowlist)

    def start(self) -> asyncio.Task | None:
        if not self.should_start():
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name="healbite-weight-reminder-scheduler")
        _log_marker(
            "weight_reminder_scheduler",
            route="weight_reminder",
            action="start",
            outcome="started",
            enabled=True,
        )
        return self._task

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _log_marker("weight_reminder_scheduler", route="weight_reminder", action="stop", outcome="stopped")

    async def run(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_marker(
                    "weight_reminder_scheduler",
                    route="weight_reminder",
                    action="scan",
                    outcome="failed",
                    error_type=_safe_error_type(exc.__class__.__name__),
                )
            await self.sleep_fn(float(self.config.scan_interval_seconds))

    async def run_once(self) -> WeightReminderSchedulerStats:
        stats = WeightReminderSchedulerStats(circuit_open=self._circuit_open)
        if not self.config.enabled or not self.config.allowlist:
            return stats
        now = self.now_fn()
        recovered = self.store.recover_expired_claims(now_utc=now)
        if recovered.get("claimed") or recovered.get("sending"):
            _log_marker(
                "weight_reminder_due",
                route="weight_reminder",
                action="claim_recovered",
                outcome="recovered",
                claim_recovered=True,
                due_count_bucket=_count_bucket(
                    int(recovered.get("claimed", 0)) + int(recovered.get("sending", 0))
                ),
            )
        if self._circuit_open:
            _log_marker(
                "weight_reminder_scheduler",
                route="weight_reminder",
                action="scan",
                outcome="circuit_open",
                enabled=True,
            )
            return stats
        due = self.store.list_due_settings(
            now_utc=now,
            allowlist=self.config.allowlist,
            batch_size=self.config.batch_size,
        )
        stats.scanned = len(due)
        if due:
            _log_marker(
                "weight_reminder_due",
                route="weight_reminder",
                action="due_detected",
                outcome="detected",
                due_count_bucket=_count_bucket(len(due)),
                batch_size_bucket=_count_bucket(self.config.batch_size),
            )
        for setting in due:
            if self._circuit_open:
                break
            claim = self.store.claim_due_setting(
                setting,
                now_utc=self.now_fn(),
                claim_lease_seconds=self.config.claim_lease_seconds,
                missed_grace_hours=self.config.missed_grace_hours,
                max_attempts=self.config.max_attempts,
            )
            if claim.outcome is ReminderClaimOutcome.CLAIMED and claim.delivery is not None:
                stats.claimed += 1
                _log_marker(
                    "weight_reminder_due",
                    route="weight_reminder",
                    action="claim_acquired",
                    outcome="claimed",
                    delivery_state=claim.delivery.status.value,
                    attempt_count_bucket=_attempt_bucket(claim.delivery.attempt_count),
                )
                await self._process_claim(claim, stats)
            elif claim.outcome is ReminderClaimOutcome.SKIPPED_EXPIRED:
                stats.skipped += 1
                _log_marker(
                    "weight_reminder_skip",
                    route="weight_reminder",
                    action="expired",
                    outcome="skipped",
                    delivery_attempted=False,
                )
            else:
                _log_marker(
                    "weight_reminder_due",
                    route="weight_reminder",
                    action="claim_not_acquired",
                    outcome=claim.outcome.value,
                    delivery_attempted=False,
                )
        if due:
            _log_marker(
                "weight_reminder_scheduler",
                route="weight_reminder",
                action="tick_complete",
                outcome="completed",
                due_count_bucket=_count_bucket(stats.scanned),
                batch_size_bucket=_count_bucket(self.config.batch_size),
                delivery_attempted=stats.claimed > 0,
                delivery_completed=stats.sent > 0,
                retry_scheduled=stats.retry_scheduled > 0,
                permanent_failure=stats.permanent_failed > 0,
                ambiguous_delivery=stats.ambiguous > 0,
            )
        return stats

    async def _process_claim(self, claim: WeightReminderClaim, stats: WeightReminderSchedulerStats) -> None:
        delivery = claim.delivery
        setting = claim.setting
        if delivery is None or setting is None:
            return
        fresh = self.store.get_active_setting_if_current(
            user_id=setting.user_id,
            schedule_version=delivery.schedule_version,
        )
        if (
            fresh is None
            or not fresh.enabled
            or fresh.delivery_state is not ReminderDeliveryState.ACTIVE
            or fresh.user_id not in self.config.allowlist
        ):
            self.store.mark_delivery_skipped_stale_and_advance(
                delivery.id,
                setting=fresh,
                now_utc=self.now_fn(),
                error_type="stale_schedule",
            )
            stats.skipped += 1
            _log_marker(
                "weight_reminder_skip",
                route="weight_reminder",
                action="stale",
                outcome="skipped_stale",
                stale_schedule=True,
                delivery_attempted=False,
            )
            return
        sending = self.store.mark_delivery_sending(delivery.id, now_utc=self.now_fn())
        _log_marker(
            "weight_reminder_delivery",
            route="weight_reminder",
            action="attempt_started",
            outcome="attempt_started",
            delivery_attempted=True,
            timezone_present=bool(fresh.timezone_name),
            timezone_region_bucket=_timezone_region_bucket(fresh.timezone_name),
            weekday_bucket=str(fresh.weekday),
            time_bucket=_time_bucket(fresh.local_time),
            attempt_count_bucket=_attempt_bucket(sending.attempt_count),
            corr_present=False,
        )
        try:
            await self.send_reminder(fresh.user_id, WEIGHT_REMINDER_TEXT)
        except Exception as exc:
            await self._handle_send_failure(sending, fresh, classify_send_exception(exc), stats)
            return
        self.store.mark_delivery_sent_and_advance(sending.id, setting=fresh, now_utc=self.now_fn())
        stats.sent += 1
        _log_marker(
            "weight_reminder_delivery",
            route="weight_reminder",
            action="send",
            outcome="sent",
            delivery_completed=True,
            attempt_count_bucket=_attempt_bucket(sending.attempt_count),
            corr_present=False,
        )

    async def _handle_send_failure(
        self,
        delivery,
        setting: WeightReminderSetting,
        error: WeightReminderDeliveryError,
        stats: WeightReminderSchedulerStats,
    ) -> None:
        now = self.now_fn()
        if error.kind is WeightReminderFailureKind.GLOBAL_AUTH:
            self._circuit_open = True
            stats.circuit_open = True
            self.store.mark_delivery_retry_wait(
                delivery.id,
                error_type=error.error_type,
                next_attempt_at_utc=now + timedelta(seconds=self.config.base_backoff_seconds),
                now_utc=now,
            )
            _log_marker(
                "weight_reminder_scheduler",
                route="weight_reminder",
                action="circuit_breaker_open",
                outcome="open",
                error_type=error.error_type,
                delivery_completed=False,
            )
            return
        if error.kind is WeightReminderFailureKind.AMBIGUOUS:
            self.store.mark_delivery_unknown_and_advance(
                delivery.id,
                setting=setting,
                now_utc=now,
                error_type=error.error_type,
            )
            stats.ambiguous += 1
            _log_marker(
                "weight_reminder_delivery",
                route="weight_reminder",
                action="send",
                outcome="delivery_unknown",
                ambiguous_delivery=True,
                error_type=error.error_type,
            )
            return
        if error.kind is WeightReminderFailureKind.PERMANENT:
            self.store.mark_delivery_permanent_failed(
                delivery.id,
                setting=setting,
                now_utc=now,
                error_type=error.error_type,
                suspend_user=True,
            )
            stats.permanent_failed += 1
            _log_marker(
                "weight_reminder_delivery",
                route="weight_reminder",
                action="send",
                outcome="permanent_failed",
                permanent_failure=True,
                error_type=error.error_type,
            )
            return
        if delivery.attempt_count >= self.config.max_attempts:
            self.store.mark_delivery_permanent_failed(
                delivery.id,
                setting=setting,
                now_utc=now,
                error_type="max_attempts",
                suspend_user=False,
            )
            stats.permanent_failed += 1
            _log_marker(
                "weight_reminder_delivery",
                route="weight_reminder",
                action="send",
                outcome="permanent_failed",
                permanent_failure=True,
                error_type="max_attempts",
                attempt_count_bucket=_attempt_bucket(delivery.attempt_count),
            )
            return
        delay = error.retry_after_seconds or min(
            self.config.base_backoff_seconds * (2 ** max(0, delivery.attempt_count - 1)),
            self.config.base_backoff_seconds * 8,
        )
        self.store.mark_delivery_retry_wait(
            delivery.id,
            error_type=error.error_type,
            next_attempt_at_utc=now + timedelta(seconds=int(delay)),
            now_utc=now,
        )
        stats.retry_scheduled += 1
        _log_marker(
            "weight_reminder_delivery",
            route="weight_reminder",
            action="retry_scheduled",
            outcome="retry_scheduled",
            retry_scheduled=True,
            error_type=error.error_type,
            attempt_count_bucket=_attempt_bucket(delivery.attempt_count),
        )


def build_weight_reminder_scheduler(
    *,
    send_reminder: Callable[[int, str], Awaitable[None]],
    db_path=None,
    env: dict[str, str] | None = None,
) -> WeightReminderScheduler | None:
    config = load_weight_reminder_config(env)
    if not config.enabled or not config.allowlist:
        return None
    return WeightReminderScheduler(
        store=HealBiteWeightReminderStore(db_path=db_path),
        config=config,
        send_reminder=send_reminder,
    )
