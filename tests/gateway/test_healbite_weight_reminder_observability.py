
from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import hermes_logging
import pytest

from gateway.healbite_weight_reminder_scheduler import (
    WEIGHT_REMINDER_TEXT,
    WeightReminderDeliveryError,
    WeightReminderFailureKind,
    WeightReminderScheduler,
)
from gateway.healbite_weight_reminders import (
    ReminderDeliveryStatus,
    WeightReminderConfig,
    HealBiteWeightReminderStore,
)

NOW = datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)
FORBIDDEN_CANARIES = {
    "PII_REMINDER_USER_900001",
    "PII_REMINDER_TELEGRAM_900002",
    "PII_REMINDER_ALLOWLIST_900003",
    "PII/Reminder/Timezone",
    "09:37",
    "PII_REMINDER_DELIVERY_KEY",
    "PII_REMINDER_CALLBACK_PAYLOAD",
    "PII_REMINDER_EXCEPTION_BODY",
    "PII_REMINDER_MESSAGE_TEXT",
    WEIGHT_REMINDER_TEXT,
}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def _config(*, enabled=True, allowlist=frozenset({101}), interval=60):
    return WeightReminderConfig(
        enabled=enabled,
        allowlist=allowlist,
        scan_interval_seconds=interval,
        missed_grace_hours=12,
        claim_lease_seconds=300,
        batch_size=50,
        max_attempts=3,
        base_backoff_seconds=30,
    )


def _store(tmp_path: Path):
    return HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db")


def _due_setting(store, user_id=101, *, now=NOW, local_time="09:00"):
    return store.create_or_update_settings(
        user_id=user_id,
        timezone_name="UTC",
        weekday=now.weekday(),
        local_time=local_time,
        enabled=True,
        now_utc=now - timedelta(days=7),
    )


def _messages(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _assert_private_values_absent(text: str) -> None:
    for value in FORBIDDEN_CANARIES:
        assert value not in text
    assert "delivery_key" not in text
    assert "user_id" not in text
    assert "telegram_id" not in text
    assert "chat_id" not in text


@pytest.mark.asyncio
async def test_feature_disabled_start_does_not_emit_false_start_marker(tmp_path, caplog):
    scheduler = WeightReminderScheduler(
        store=_store(tmp_path),
        config=_config(enabled=False),
        send_reminder=lambda *_: None,
        now_fn=Clock(),
    )

    with caplog.at_level(logging.INFO):
        assert scheduler.start() is None
        await scheduler.run_once()

    text = _messages(caplog)
    assert "[HealBite][weight_reminder_scheduler]" not in text
    assert "action=start" not in text


@pytest.mark.asyncio
async def test_successful_delivery_emits_safe_due_delivery_and_tick_markers(tmp_path, caplog):
    store = _store(tmp_path)
    _due_setting(store)

    async def send(_user_id, _text):
        return None

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    with caplog.at_level(logging.INFO):
        await scheduler.run_once()

    delivery = store.list_deliveries(101)[0]
    assert delivery.status is ReminderDeliveryStatus.SENT
    text = _messages(caplog)
    assert "[HealBite][weight_reminder_due]" in text
    assert "action=due_detected" in text
    assert "action=claim_acquired" in text
    assert "[HealBite][weight_reminder_delivery]" in text
    assert "action=attempt_started" in text
    assert "outcome=sent" in text
    assert "[HealBite][weight_reminder_scheduler]" in text
    assert "action=tick_complete" in text
    _assert_private_values_absent(text)


@pytest.mark.asyncio
async def test_duplicate_prevention_does_not_emit_second_sent_marker(tmp_path, caplog):
    store = _store(tmp_path)
    _due_setting(store)
    calls = 0

    async def send(_user_id, _text):
        nonlocal calls
        calls += 1

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    with caplog.at_level(logging.INFO):
        await scheduler.run_once()
        await scheduler.run_once()

    assert calls == 1
    text = _messages(caplog)
    assert text.count("outcome=sent") == 1


@pytest.mark.asyncio
async def test_retry_unknown_permanent_and_circuit_breaker_markers(tmp_path, caplog):
    cases = [
        (WeightReminderFailureKind.TRANSIENT, "network_error", "retry_scheduled"),
        (WeightReminderFailureKind.AMBIGUOUS, "send_timeout", "delivery_unknown"),
        (WeightReminderFailureKind.PERMANENT, "blocked_user", "permanent_failed"),
        (WeightReminderFailureKind.GLOBAL_AUTH, "bot_auth_failed", "circuit_breaker_open"),
    ]
    for index, (kind, error_type, expected) in enumerate(cases, start=1):
        store = _store(tmp_path / str(index))
        _due_setting(store)

        async def send(_user_id, _text, *, _kind=kind, _error_type=error_type):
            raise WeightReminderDeliveryError(_kind, error_type=_error_type)

        scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
        with caplog.at_level(logging.INFO):
            await scheduler.run_once()

        text = _messages(caplog)
        assert expected in text
        assert "PII_REMINDER_EXCEPTION_BODY" not in text
        caplog.clear()


@pytest.mark.asyncio
async def test_expired_and_stale_skip_markers_are_safe(tmp_path, caplog):
    store = _store(tmp_path)
    _due_setting(store, now=NOW - timedelta(days=7))
    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=lambda *_: None, now_fn=Clock())
    with caplog.at_level(logging.INFO):
        await scheduler.run_once()
    assert "[HealBite][weight_reminder_skip]" in _messages(caplog)
    assert "action=expired" in _messages(caplog)

    caplog.clear()
    store2 = _store(tmp_path / "stale")
    setting = _due_setting(store2)
    claim = store2.claim_due_setting(setting, now_utc=NOW)
    store2.disable_settings(101, now_utc=NOW)
    scheduler2 = WeightReminderScheduler(store=store2, config=_config(), send_reminder=lambda *_: None, now_fn=Clock())
    with caplog.at_level(logging.INFO):
        await scheduler2._process_claim(claim, await scheduler2.run_once())
    text = _messages(caplog)
    assert "action=stale" in text
    assert "outcome=skipped_stale" in text
    _assert_private_values_absent(text)


def _install_file_logging(tmp_path: Path):
    root = logging.getLogger()
    existing = list(root.handlers)
    previous_initialized = hermes_logging._logging_initialized
    hermes_logging.setup_logging(
        hermes_home=tmp_path,
        mode="gateway",
        force=True,
        log_level="INFO",
    )

    def cleanup():
        for handler in list(root.handlers):
            if handler not in existing:
                handler.flush()
                handler.close()
                root.removeHandler(handler)
        hermes_logging._logging_initialized = previous_initialized

    return cleanup


def _read_file_logs(tmp_path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    chunks: list[str] = []
    for name in ("agent.log", "gateway.log", "errors.log"):
        path = tmp_path / "logs" / name
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_file_log_privacy_replay_keeps_safe_markers_without_canaries(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        store = _store(tmp_path / "db")
        _due_setting(store)

        async def send(_user_id, _text):
            return None

        scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
        await scheduler.run_once()
        text = _read_file_logs(tmp_path)
    finally:
        cleanup()

    assert "[HealBite][weight_reminder_due]" in text
    assert "[HealBite][weight_reminder_delivery]" in text
    assert "[HealBite][weight_reminder_scheduler]" in text
    _assert_private_values_absent(text)


@dataclass(frozen=True)
class SafeLogEvent:
    sink: str
    marker: str
    action: str
    outcome: str
    error_type: str = "none"
    attempt_count_bucket: str = "1"
    corr_present: bool = False
    window: str = "w1"

    def signature(self) -> tuple[str, str, str, str, str, bool, str]:
        return (
            self.marker,
            self.action,
            self.outcome,
            self.error_type,
            self.attempt_count_bucket,
            self.corr_present,
            self.window,
        )


def logical_event_count(events: list[SafeLogEvent]) -> int:
    per_signature_sink: dict[tuple[str, str, str, str, str, bool, str], Counter[str]] = defaultdict(Counter)
    for event in events:
        per_signature_sink[event.signature()][event.sink] += 1
    return sum(max(counter.values()) for counter in per_signature_sink.values())


def test_cross_sink_logical_event_counting_uses_signature_not_corr_only():
    same_record_two_sinks = [
        SafeLogEvent("agent.log", "delivery", "send", "sent"),
        SafeLogEvent("gateway.log", "delivery", "send", "sent"),
    ]
    assert logical_event_count(same_record_two_sinks) == 1

    distinct_events_same_corr = [
        SafeLogEvent("agent.log", "delivery", "attempt_started", "attempt_started", corr_present=True),
        SafeLogEvent("agent.log", "delivery", "send", "sent", corr_present=True),
    ]
    assert logical_event_count(distinct_events_same_corr) == 2


@pytest.mark.asyncio
async def test_gateway_lifecycle_feature_off_does_not_emit_scheduler_start(monkeypatch, caplog):
    from gateway.run import GatewayRunner

    monkeypatch.delenv("WEIGHT_REMINDERS_ENABLED", raising=False)
    monkeypatch.delenv("WEIGHT_REMINDERS_ALLOWLIST", raising=False)
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}

    with caplog.at_level(logging.INFO):
        await GatewayRunner._start_weight_reminder_scheduler_if_configured(runner)

    text = _messages(caplog)
    assert "[HealBite][weight_reminder_scheduler]" not in text
    assert "action=start" not in text


@pytest.mark.asyncio
async def test_gateway_lifecycle_no_telegram_adapter_uses_config_marker(monkeypatch, caplog):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("WEIGHT_REMINDERS_ENABLED", "true")
    monkeypatch.setenv("WEIGHT_REMINDERS_ALLOWLIST", "101")
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}

    with caplog.at_level(logging.INFO):
        await GatewayRunner._start_weight_reminder_scheduler_if_configured(runner)

    text = _messages(caplog)
    assert "[HealBite][weight_reminder_config]" in text
    assert "outcome=no_telegram_adapter" in text
    assert "[HealBite][weight_reminder_scheduler]" not in text
