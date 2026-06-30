from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gateway.healbite_weight_reminder_scheduler import (
    WEIGHT_REMINDER_TEXT,
    WeightReminderDeliveryError,
    WeightReminderFailureKind,
    WeightReminderScheduler,
)
from gateway.healbite_weight_reminders import (
    ReminderDeliveryState,
    ReminderDeliveryStatus,
    WeightReminderConfig,
    HealBiteWeightReminderStore,
    WEIGHT_REMINDER_DELIVERIES_TABLE,
    WEIGHT_REMINDER_SETTINGS_TABLE,
)


NOW = datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)


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


def _store(tmp_path):
    return HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db")


def _due_setting(store, user_id=101, *, now=NOW, enabled=True, version_time="09:00"):
    return store.create_or_update_settings(
        user_id=user_id,
        timezone_name="UTC",
        weekday=now.weekday(),
        local_time=version_time,
        enabled=enabled,
        now_utc=now - timedelta(days=7),
    )


async def _noop_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_feature_off_and_empty_allowlist_do_not_scan_or_send(tmp_path):
    store = _store(tmp_path)
    _due_setting(store)
    calls = []

    async def send(user_id, text):
        calls.append((user_id, text))

    disabled = WeightReminderScheduler(store=store, config=_config(enabled=False), send_reminder=send)
    empty = WeightReminderScheduler(store=store, config=_config(allowlist=frozenset()), send_reminder=send)

    assert disabled.start() is None
    assert empty.start() is None
    assert (await disabled.run_once()).scanned == 0
    assert (await empty.run_once()).scanned == 0
    assert calls == []
    assert store.list_deliveries(101) == []


@pytest.mark.asyncio
async def test_scheduler_starts_once_and_shutdown_cancels(tmp_path):
    store = _store(tmp_path)
    calls = []
    gate = asyncio.Event()

    async def send(user_id, text):
        calls.append(user_id)
        gate.set()

    scheduler = WeightReminderScheduler(
        store=store,
        config=_config(interval=3600),
        send_reminder=send,
        sleep_fn=asyncio.sleep,
    )
    task1 = scheduler.start()
    task2 = scheduler.start()
    assert task1 is task2
    await scheduler.stop()
    assert task1 is not None
    assert task1.done()


@pytest.mark.asyncio
async def test_successful_send_transitions_to_sent_and_advances_next_due(tmp_path):
    store = _store(tmp_path)
    _due_setting(store)
    calls = []

    async def send(user_id, text):
        calls.append((user_id, text))

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    stats = await scheduler.run_once()
    deliveries = store.list_deliveries(101)
    setting = store.get_settings(101)

    assert stats.sent == 1
    assert calls == [(101, WEIGHT_REMINDER_TEXT)]
    assert deliveries[0].status is ReminderDeliveryStatus.SENT
    assert deliveries[0].attempt_count == 1
    assert setting is not None
    assert setting.last_delivered_at_utc is not None
    assert setting.next_due_at_utc == "2026-07-07 09:00:00"


@pytest.mark.asyncio
async def test_two_scheduler_instances_same_db_send_once(tmp_path):
    store = _store(tmp_path)
    _due_setting(store)
    calls = []

    async def send(user_id, text):
        calls.append(user_id)

    first = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    second = WeightReminderScheduler(store=HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db"), config=_config(), send_reminder=send, now_fn=Clock())
    await asyncio.gather(first.run_once(), second.run_once())

    assert calls == [101]
    assert len(store.list_deliveries(101)) == 1


@pytest.mark.asyncio
async def test_stale_schedule_disable_and_suspend_after_claim_skip_send(tmp_path):
    store = _store(tmp_path)
    setting = _due_setting(store)
    claim = store.claim_due_setting(setting, now_utc=NOW)
    assert claim.delivery is not None
    store.create_or_update_settings(
        user_id=101,
        timezone_name="UTC",
        weekday=(NOW.weekday() + 1) % 7,
        local_time="09:00",
        enabled=True,
        now_utc=NOW,
    )
    calls = []

    async def send(user_id, text):
        calls.append(user_id)

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    await scheduler._process_claim(claim, scheduler_stats := await scheduler.run_once())

    assert calls == []
    assert store.list_deliveries(101)[0].status is ReminderDeliveryStatus.SKIPPED_STALE


@pytest.mark.asyncio
async def test_disable_after_claim_skips_send(tmp_path):
    store = _store(tmp_path)
    setting = _due_setting(store)
    claim = store.claim_due_setting(setting, now_utc=NOW)
    assert claim.delivery is not None
    store.disable_settings(101, now_utc=NOW)
    calls = []

    async def send(user_id, text):
        calls.append(user_id)

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    await scheduler._process_claim(claim, await scheduler.run_once())
    assert calls == []
    assert store.list_deliveries(101)[0].status is ReminderDeliveryStatus.SKIPPED_STALE


@pytest.mark.asyncio
async def test_suspend_after_claim_skips_send(tmp_path):
    store = _store(tmp_path)
    setting = _due_setting(store)
    claim = store.claim_due_setting(setting, now_utc=NOW)
    store.suspend_delivery(101, safe_reason="manual_suspend", now_utc=NOW)
    calls = []

    async def send(user_id, text):
        calls.append(user_id)

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    await scheduler._process_claim(claim, await scheduler.run_once())
    assert calls == []
    assert store.list_deliveries(101)[0].status is ReminderDeliveryStatus.SKIPPED_STALE


@pytest.mark.asyncio
async def test_transient_retry_rate_limit_and_eventual_success(tmp_path):
    store = _store(tmp_path)
    _due_setting(store)
    clock = Clock()
    calls = 0

    async def send(user_id, text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WeightReminderDeliveryError(WeightReminderFailureKind.TRANSIENT, error_type="network_error")
        if calls == 2:
            raise WeightReminderDeliveryError(
                WeightReminderFailureKind.RATE_LIMIT,
                error_type="rate_limited",
                retry_after_seconds=45,
            )

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=clock)
    await scheduler.run_once()
    assert store.list_deliveries(101)[0].status is ReminderDeliveryStatus.RETRY_WAIT
    clock.advance(seconds=30)
    await scheduler.run_once()
    delivery = store.list_deliveries(101)[0]
    assert delivery.status is ReminderDeliveryStatus.RETRY_WAIT
    assert delivery.next_attempt_at_utc == "2026-06-30 09:01:15"
    clock.advance(seconds=45)
    await scheduler.run_once()
    delivery = store.list_deliveries(101)[0]
    assert delivery.status is ReminderDeliveryStatus.SENT
    assert delivery.attempt_count == 3


@pytest.mark.asyncio
async def test_ambiguous_send_does_not_retry_and_advances(tmp_path):
    store = _store(tmp_path)
    _due_setting(store)

    async def send(user_id, text):
        raise WeightReminderDeliveryError(WeightReminderFailureKind.AMBIGUOUS, error_type="send_timeout")

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    await scheduler.run_once()
    delivery = store.list_deliveries(101)[0]
    setting = store.get_settings(101)
    assert delivery.status is ReminderDeliveryStatus.DELIVERY_UNKNOWN
    assert setting is not None
    assert setting.next_due_at_utc == "2026-07-07 09:00:00"
    await scheduler.run_once()
    assert store.list_deliveries(101)[0].attempt_count == 1


def test_expired_claim_recovery_distinguishes_claimed_and_sending(tmp_path):
    store = _store(tmp_path)
    setting = _due_setting(store)
    first = store.claim_due_setting(setting, now_utc=NOW, claim_lease_seconds=1)
    assert first.delivery is not None
    store.mark_delivery_sending(first.delivery.id, now_utc=NOW)
    recovered = store.recover_expired_claims(now_utc=NOW + timedelta(seconds=2))
    assert recovered["sending"] == 1
    assert store.list_deliveries(101)[0].status is ReminderDeliveryStatus.DELIVERY_UNKNOWN

    store.create_or_update_settings(
        user_id=202,
        timezone_name="UTC",
        weekday=NOW.weekday(),
        local_time="09:00",
        enabled=True,
        now_utc=NOW - timedelta(days=7),
    )
    second = store.claim_due_setting(store.get_settings(202), now_utc=NOW, claim_lease_seconds=1)
    assert second.delivery is not None
    recovered = store.recover_expired_claims(now_utc=NOW + timedelta(seconds=2))
    assert recovered["claimed"] == 1
    assert store.list_deliveries(202)[0].status is ReminderDeliveryStatus.PENDING


@pytest.mark.asyncio
async def test_permanent_failure_suspends_user_but_preserves_enabled(tmp_path):
    store = _store(tmp_path)
    _due_setting(store)

    async def send(user_id, text):
        raise WeightReminderDeliveryError(WeightReminderFailureKind.PERMANENT, error_type="blocked_user")

    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    await scheduler.run_once()
    setting = store.get_settings(101)
    delivery = store.list_deliveries(101)[0]
    assert delivery.status is ReminderDeliveryStatus.PERMANENT_FAILED
    assert setting is not None
    assert setting.enabled is True
    assert setting.delivery_state is ReminderDeliveryState.SUSPENDED
    assert setting.suspension_reason == "blocked_user"


@pytest.mark.asyncio
async def test_global_auth_failure_opens_breaker_without_user_mutation(tmp_path):
    store = _store(tmp_path)
    _due_setting(store, user_id=101)
    _due_setting(store, user_id=202)
    calls = []

    async def send(user_id, text):
        calls.append(user_id)
        raise WeightReminderDeliveryError(WeightReminderFailureKind.GLOBAL_AUTH, error_type="bot_auth_failed")

    scheduler = WeightReminderScheduler(
        store=store,
        config=_config(allowlist=frozenset({101, 202})),
        send_reminder=send,
        now_fn=Clock(),
    )
    await scheduler.run_once()
    assert calls == [101]
    assert scheduler.circuit_open is True
    assert store.get_settings(101).delivery_state is ReminderDeliveryState.ACTIVE
    await scheduler.run_once()
    assert calls == [101]


@pytest.mark.asyncio
async def test_missed_occurrence_skips_expired_without_backlog(tmp_path):
    store = _store(tmp_path)
    _due_setting(store, now=NOW - timedelta(days=7))
    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=lambda *_: None, now_fn=Clock())

    await scheduler.run_once()
    delivery = store.list_deliveries(101)[0]
    assert delivery.status is ReminderDeliveryStatus.SKIPPED
    assert delivery.last_error_type == "missed_window"
    assert len(store.list_deliveries(101)) == 1


@pytest.mark.asyncio
async def test_allowlist_excludes_user_without_mutation(tmp_path):
    store = _store(tmp_path)
    _due_setting(store, user_id=101)
    calls = []

    async def send(user_id, text):
        calls.append(user_id)

    scheduler = WeightReminderScheduler(
        store=store,
        config=_config(allowlist=frozenset({202})),
        send_reminder=send,
        now_fn=Clock(),
    )
    await scheduler.run_once()
    assert calls == []
    assert store.list_deliveries(101) == []


def test_delivery_text_is_deterministic_and_contains_no_health_value():
    assert WEIGHT_REMINDER_TEXT == "\u041f\u043e\u0440\u0430 \u0437\u0430\u043f\u0438\u0441\u0430\u0442\u044c \u0432\u0435\u0441\n\n\u0420\u0435\u0433\u0443\u043b\u044f\u0440\u043d\u044b\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043f\u043e\u043c\u043e\u0433\u0430\u044e\u0442 \u043e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u0442\u044c \u0434\u0438\u043d\u0430\u043c\u0438\u043a\u0443."
    assert "\u043a\u0433" not in WEIGHT_REMINDER_TEXT


def test_safe_markers_do_not_emit_identifiers_or_payloads(tmp_path, caplog):
    store = _store(tmp_path)
    _due_setting(store)

    async def send(user_id, text):
        return None

    caplog.set_level(logging.INFO)
    scheduler = WeightReminderScheduler(store=store, config=_config(), send_reminder=send, now_fn=Clock())
    asyncio.run(scheduler.run_once())

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "[HealBite][weight_reminder_due]" in combined
    assert "[HealBite][weight_reminder_delivery]" in combined
    assert "101" not in combined
    assert "UTC" not in combined
    assert WEIGHT_REMINDER_TEXT not in combined
    assert "delivery_key" not in combined


def test_schema_migration_adds_retry_columns_idempotently(tmp_path):
    db = tmp_path / "healbite.db"
    store = HealBiteWeightReminderStore(db_path=db)
    store = HealBiteWeightReminderStore(db_path=db)
    with sqlite3.connect(db) as conn:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({WEIGHT_REMINDER_DELIVERIES_TABLE})")}
        assert "next_attempt_at_utc" in columns
        assert "schedule_version" in columns
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
