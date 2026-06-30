# ADR: Weight Reminder Scheduler

Status: Proposed

Date: 2026-06-30

## Context

HealBite supports weight entry, append-only weight history, profile
synchronization, macro recalculation, water tracking, and Telegram privacy
hardening. Weekly weight reminders are not implemented in the reviewed target.

The current UI contains only a safe unavailable reminder placeholder. Reminder
tables, scheduler runtime, delivery outbox, and user reminder settings are not
present.

The reminder system must avoid generic Hermes agent dispatch, LLM providers,
dangerous tools, and approval-triggering paths.

## Decision

Use an in-process async periodic scanner inside `hermes-bot`.

The scheduler will use:

- SQLite reminder settings;
- SQLite delivery outbox;
- claim lease for concurrency protection;
- unique delivery key for idempotency;
- IANA timezone wall-clock scheduling;
- feature flag and rollout allowlist;
- deterministic Telegram messages without LLM calls.

Default production state is disabled.

## Alternatives Considered

### Server cron without user timezone awareness

Rejected. It cannot safely represent per-user wall-clock schedules and missed
reminder behavior.

### APScheduler or equivalent with state held only in memory

Rejected. In-memory state is not restart-safe and does not provide enough
duplicate-delivery protection.

### Separate Compose worker

Deferred. It may be useful later, but it changes deployment topology and is
unnecessary for the current scale. SQLite claim/outbox semantics protect
against accidental duplicate schedulers.

### LLM-generated reminder content

Rejected. Reminder content must be deterministic and must not invoke providers
or tools.

### UTC-offset-only timezone storage

Rejected. UTC offsets do not handle DST or timezone database updates.

## Failure Model

Known failure windows:

- process crashes before send;
- process crashes during send;
- Telegram send succeeds but DB update fails;
- two scheduler instances attempt the same occurrence;
- settings change during a claim;
- Telegram returns transient or permanent delivery errors.

Mitigation:

- unique delivery key;
- claim lease;
- bounded retry;
- permanent failure classification;
- schedule version;
- feature flag emergency stop.

Telegram does not provide a complete distributed exactly-once guarantee. The
system targets at-most-one recorded successful delivery and practical duplicate
suppression.

## Security and Privacy Impact

The scheduler must:

- derive user identity from existing authenticated context/store records;
- keep callback payloads enum-based;
- avoid user IDs in callback payloads;
- never expose dangerous tools;
- never call LLM/provider code;
- log only safe buckets and sanitized error types;
- avoid raw identifiers, health values, callback data, message text, and raw
  exception bodies.

## Rollback Compatibility

Reminder tables are additive. Older builds ignore them.

Emergency rollback should first disable:

```text
WEIGHT_REMINDERS_ENABLED=false
```

If image rollback is needed, reminder tables remain in SQLite and existing
weight/profile/water/nutrition flows continue to operate.

## Consequences

Positive:

- no new production service for MVP;
- deterministic product-lane behavior;
- restart-safe state;
- privacy-safe rollout path;
- simple flag-off rollback.

Costs:

- requires careful SQLite transaction design;
- requires timezone and DST tests;
- requires duplicate-delivery smoke;
- requires privacy tests for scheduler, callbacks, and delivery markers.
