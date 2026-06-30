# Sprint 7.0C2 - Weekly Weight Reminder Delivery

Status: design-only proposal. This document does not implement runtime code,
database migrations, tests, Docker changes, deployment, restart, or production
configuration changes.

Base SHA: `413878ba4f9123978de615f3e382b7be53eb31d1`

## Existing State Reviewed

Sprint 7.0C1 is deployed and verified in production. It provides weight entry,
append-only weight history, profile weight synchronization, macro target
recalculation, Telegram weight UX, privacy hardening, and safe tool gating.

The current Sprint 7.0C2 baseline has these reminder-related facts:

- `gateway/healbite_weight_tracker.py` owns `weight_entries` and
  `weight_pending_inputs`.
- Reminder tables are not present in the reviewed baseline.
- Reminder scheduler and delivery runtime are not present.
- User reminder settings are not present.
- The Telegram `weight:reminder` callback is a neutral unavailable placeholder.
- The reminder button is not exposed as an active production feature.
- `gateway/healbite_time.py` has timezone-aware local-day helpers.
- The profile schema does not currently expose a canonical user timezone field.
- `gateway/run.py` already has background watcher lifecycle patterns suitable
  for an in-process deterministic scheduler.
- Existing Sprint 7.0 architecture notes reject generic LLM cron prompts for
  health reminders.

The previous short note said reminder storage and due/dedupe helpers existed.
That was stale for this target. Sprint 7.0C2 must add reminder schema and
delivery helpers before any scheduler is enabled.

## Product Contract

Weekly weight reminders are opt-in only.

Default state:

- disabled;
- no automatic opt-in;
- no delivery before explicit confirmation;
- no global enablement without a separate rollout approval.

A user can:

- enable a weekly reminder;
- disable the reminder;
- select weekday;
- select local time;
- confirm timezone;
- change settings;
- view current settings;
- return without saving.

The weight screen may expose a `Напоминание` button only after the feature is
implemented behind feature flag and allowlist.

Expected settings screen after implementation:

```text
Напоминание о взвешивании

Статус: включено
День: воскресенье
Время: 09:00
Часовой пояс: Europe/Berlin
```

## Recommended Scheduler Architecture

Use an in-process async periodic scanner inside `hermes-bot`, backed by SQLite
claim/outbox state.

Recommended shape:

- one scheduler task per Hermes process;
- feature flag disabled by default;
- rollout allowlist required before delivery;
- deterministic Telegram delivery without LLM/provider calls;
- SQLite-level claim and idempotency protection;
- no separate Compose worker for the first implementation.

A separate worker is unnecessary at the current scale because it changes
topology and deploy complexity. SQLite claim/outbox semantics still protect
against accidental multi-process execution.

Generic cron/agent routes are rejected because they can involve LLM/tool
execution and are too broad for health reminders.

## Scheduler Lifecycle

Start:

- after DB initialization is available;
- after Telegram adapter initialization succeeds;
- only if `WEIGHT_REMINDERS_ENABLED=true`;
- only after feature-flag and allowlist config load.

Tick:

- run every `WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS`, default 60 seconds;
- select due settings in bounded batches;
- atomically claim due occurrence;
- create or reuse outbox row;
- send deterministic Telegram message;
- record result;
- calculate next future occurrence.

Stop:

- stop through graceful gateway shutdown;
- cancel and await background task;
- do not block polling loop indefinitely;
- do not start new sends after cancellation;
- let in-flight sends finish within a short timeout or expire the claim.

## Multi-Process Locking Strategy

Do not assume there is always only one process.

SQLite must be the coordination boundary:

- claim due occurrence inside a transaction;
- use a delivery outbox row with a unique delivery key;
- set `claimed_at_utc` and `claim_expires_at_utc`;
- skip unexpired claims owned by another tick/process;
- retry expired claims;
- mark `sent` only after Telegram send succeeds.

Required invariants:

```text
duplicate_delivery=false
cross_user_delivery=false
stale_setting_delivery=false
```

## Schema Proposal

Additive-only schema. Do not alter existing weight, profile, nutrition, water,
or pending-state tables.

`weight_reminder_settings`:

```text
user_id INTEGER PRIMARY KEY
enabled INTEGER NOT NULL DEFAULT 0
timezone TEXT NOT NULL
weekday INTEGER NOT NULL
local_time TEXT NOT NULL
next_due_at_utc TEXT
schedule_version INTEGER NOT NULL DEFAULT 1
last_delivered_at_utc TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

`weight_reminder_deliveries`:

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
user_id INTEGER NOT NULL
scheduled_for_utc TEXT NOT NULL
delivery_key TEXT NOT NULL
status TEXT NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
claimed_at_utc TEXT
claim_expires_at_utc TEXT
last_error_type TEXT
sent_at_utc TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

Indexes:

- unique `(user_id, delivery_key)`;
- due scan index on settings enabled/next due;
- delivery status/next attempt index;
- user/status index for diagnostics.

Requirements:

- strict user isolation;
- idempotent initializer;
- no duplicate Telegram ID storage;
- no destructive migration;
- rollback image remains compatible because older code ignores additive tables.

## Delivery Outbox Required

A delivery outbox is required.

It protects against:

- repeated scheduler ticks;
- process restart;
- Telegram transient failure;
- send success followed by DB update failure;
- accidental second scheduler process.

The outbox is not a user-facing history screen. It is an internal delivery
state table and must not log health data or identifiers.

## Idempotency Key

One scheduled occurrence must produce at most one recorded successful delivery.

Recommended delivery key input:

```text
user
scheduled local occurrence
timezone
schedule version
```

Example shape:

```text
weight-reminder:v1:{user_id}:{local_date}:{weekday}:{local_time}:{timezone}:{schedule_version}
```

The key is stored in SQLite but must not be logged.

Telegram does not provide a full distributed exactly-once guarantee. The
implementation should use idempotent DB state, claim lease, bounded retry, and
duplicate suppression to minimize duplicate messages.

Crash windows to test:

- before send;
- during send;
- send succeeded but DB update failed;
- two workers claim simultaneously;
- process restart with expired claim;
- settings changed during claim.

## Timezone Source

Store IANA timezone names, for example:

```text
Europe/Berlin
Asia/Almaty
America/New_York
```

Do not store only UTC offsets.

Timezone source priority:

1. explicit user setting;
2. confirmed profile timezone if a canonical field is introduced later;
3. otherwise require confirmation.

Telegram locale is not a timezone source.

Server/runtime timezone can be used as a UI hint only. It must not silently
become the user's reminder timezone.

## DST Policy

Use wall-clock local scheduling and recalculate each next occurrence using the
timezone database.

Policy:

- spring-forward nonexistent time shifts to the next valid local time;
- fall-back repeated time delivers once;
- timezone change increments schedule version and recalculates
  `next_due_at_utc`;
- store both local schedule fields and UTC due/delivery timestamps.

## Missed Reminder Policy

Use a 12-hour missed reminder grace window.

Policy:

- send at most one latest missed occurrence;
- skip occurrences older than the grace window;
- never send a backlog;
- calculate the next future occurrence after skip or send.

## Retry Policy

Delivery retries are bounded.

Recommended handling:

- network timeout or 5xx: bounded exponential backoff;
- 429: respect Telegram `retry_after`;
- blocked user or chat not found: mark permanent failure and disable or suspend
  reminder;
- 401/403 bot auth: global critical condition, stop retry storm;
- unknown permanent error: record sanitized `error_type` and stop bounded
  sequence.

Never retry forever.

## Deterministic Delivery

The scheduler must not call LLM providers.

Example deterministic message:

```text
Пора записать вес

Регулярные записи помогают отслеживать динамику.
```

Buttons:

```text
Записать вес
История веса
Отключить напоминание
```

`Записать вес` starts the existing pending weight flow. It must not record a
weight automatically.

## Callback Security

Rules:

- authenticated Telegram context only;
- no user ID in callback payload;
- enum-based callback values;
- callbacks modify only the current user's settings;
- forged callback cannot target another user;
- scheduler cannot access dangerous tools;
- scheduler does not invoke provider/model.

Example safe callback families:

```text
weight_reminder:open
weight_reminder:enable
weight_reminder:disable
weight_reminder:weekday:<enum>
weight_reminder:time:<preset>
weight_reminder:skip
weight:custom
weight:history
```

## Privacy Contract

Safe events:

```text
[HealBite][weight_reminder_config]
[HealBite][weight_reminder_due]
[HealBite][weight_reminder_delivery]
[HealBite][weight_reminder_skip]
```

Safe fields:

```text
route
action
outcome
enabled
weekday_bucket
time_bucket
timezone_present
timezone_region_bucket
delivery_attempted
delivery_completed
retry_scheduled
permanent_failure
error_type
corr_present
corr
```

Do not log:

```text
user ID
Telegram ID
chat ID
username
message text
raw timezone string
weight
history
profile values
macro values
callback payload
provider body
exception body
delivery key
SQL row payload
```

## Observability Contract

The implementation should emit one low-noise marker per state transition:

- scheduler disabled/enabled at startup;
- due scan completed;
- due occurrence claimed;
- duplicate skipped;
- delivery sent;
- retry scheduled;
- permanent failure;
- setting enabled/disabled;
- scheduler stopped.

All markers must be PII-safe and must preserve the current file-log privacy
hardening.

## Feature Flag Strategy

Recommended flags:

```text
WEIGHT_REMINDERS_ENABLED=false
WEIGHT_REMINDERS_ALLOWLIST=
WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS=60
WEIGHT_REMINDER_MISSED_GRACE_HOURS=12
```

Flag-off behavior:

- no scheduler delivery;
- no reminder UI activation;
- no production reminders;
- existing weight tracking remains unchanged.

## Allowlist Strategy

Use a dedicated reminder allowlist, not admin-list.

Admin status must not imply reminder eligibility.

Logs may include only:

```text
allowlist_result=matched|not_matched
```

Do not log allowlist contents or matched identity.

## Migration Strategy

Implementation should use additive, idempotent initialization:

- pristine DB creation;
- production-derived DB migration;
- second initializer no-op;
- existing counts preserved;
- no destructive schema changes;
- no default enabled reminders.

Rollback must not require dropping reminder tables.

## Rollback Compatibility

Rollback to a pre-reminder image must be safe:

- older code ignores additive tables;
- existing weight/profile/water/nutrition flows continue;
- no scheduler runs when old image is active;
- re-deploy can resume using outbox/idempotency.

Primary emergency switch:

```text
WEIGHT_REMINDERS_ENABLED=false
```

## Future Test Plan

Schema:

- pristine creation;
- production-derived migration;
- second init idempotency;
- existing counts preserved;
- rollback image compatibility.

Settings:

- enable;
- disable;
- weekday update;
- time update;
- timezone update;
- invalid timezone;
- invalid local time;
- user isolation.

Scheduling:

- due/not due;
- DST forward;
- DST backward;
- missed within grace;
- missed outside grace;
- restart;
- expired claim;
- concurrent workers;
- settings changed after claim.

Delivery:

- success;
- timeout;
- 5xx;
- 429 retry-after;
- blocked bot;
- chat not found;
- auth error;
- send succeeded but DB update failed.

UI/security:

- callback authorization;
- forged callback;
- no cross-user change;
- back navigation;
- disabled reminder;
- existing weight pending flow reused.

Privacy:

- no IDs;
- no health values;
- no raw timezone;
- no message text;
- no callback payload;
- sanitized exceptions;
- multiple handlers safe.

## PR Split Proposal

1. Schema and service core: tables, pure scheduling calculations, store, tests,
   feature flag off, no delivery.
2. Scheduler and outbox: periodic scanner, claim lease, retry policy, safe
   observability, feature flag off.
3. UI/settings: reminder button, enable/disable, weekday/time/timezone
   configuration, callback authorization, feature flag off.
4. Production enablement: config/runbook only after explicit approval.

## Rollout Phases

1. Deploy implementation with feature disabled.
2. Verify DB integrity and existing weight flow.
3. Confirm scheduler disabled marker.
4. Enable for one allowlisted internal user.
5. Configure one near-future occurrence.
6. Verify exactly one delivery.
7. Verify no duplicate after repeated scans/restart.
8. Verify disable flow.
9. Expand allowlist gradually.
10. Consider global opt-in UI only after privacy and duplicate-delivery smoke.

## Open Questions

- Should permanent Telegram block disable reminders forever or suspend them
  until the user opens the bot again?
- Should timezone selection use fixed presets first or allow arbitrary IANA
  input in MVP?
- Should local time be arbitrary minute or fixed 15-minute slots?
- Where should reminder allowlist administration live for production operators?
