# HealBite Weight Reminders Rollout Runbook

Status: proposed. This runbook is for future Sprint 7.0C2 implementation
rollout. It must not be used to enable reminders from the docs-only design PR.

## Safety Rules

- SQLite remains the source of truth.
- Reminder delivery is deterministic.
- Reminder delivery does not use LLM providers.
- Reminder turns do not receive terminal, code execution, file, delegation, or
  approval-triggering tools.
- Reminder state is disabled by default.
- Rollout uses a dedicated allowlist, not admin-list.
- No raw identifiers, callback payloads, message text, weight values, profile
  values, macros, or exception bodies in logs.

## Feature Flags

Recommended defaults:

```text
WEIGHT_REMINDERS_ENABLED=false
WEIGHT_REMINDERS_ALLOWLIST=
WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS=60
WEIGHT_REMINDER_MISSED_GRACE_HOURS=12
```

## Stage 1 - Schema and Core

Scope:

- additive tables;
- pure scheduling calculations;
- repository/store;
- unit tests;
- feature flag off;
- no Telegram delivery.

Checks:

- pristine DB initializes;
- production-derived DB migrates;
- second init is idempotent;
- existing row counts are preserved;
- rollback image compatibility is documented.

## Stage 2 - Scheduler and Outbox

Scope:

- periodic scanner;
- claim lease;
- outbox/idempotency;
- retry policy;
- safe observability;
- feature flag off by default.

Checks:

- scheduler does not start when flag is off;
- scheduler starts once when flag is on;
- clean shutdown cancels task;
- two simulated workers do not duplicate delivery;
- expired claim can retry;
- delivery success marks one outbox row sent.

## Stage 3 - UI and Settings

Scope:

- reminder button;
- enable/disable;
- weekday selection;
- local time selection;
- timezone confirmation;
- callback authorization;
- feature flag off by default.

Checks:

- button hidden or placeholder while flag off;
- allowlisted user can open settings when flag on;
- non-allowlisted user cannot enable reminders;
- forged callback cannot change another user's settings;
- back navigation works;
- existing weight pending flow is reused.

## Stage 4 - Production Deployment With Feature Disabled

Preflight:

- confirm target SHA;
- confirm clean deploy worktree;
- confirm production baseline container/image/Qdrant;
- create backup using the normal production backup playbook;
- verify SQLite integrity;
- verify pending states are clear or explicitly allowed by rollout plan;
- verify flags remain disabled.

Deploy expectations:

```text
restart_count=0
sqlite_integrity=ok
qdrant_unchanged=true
reminder_scheduler_delivery_count=0
command_approval_count=0
dangerous_tool_count=0
privacy_canary_count=0
```

The first production deploy must not send reminders.

## Stage 5 - Allowlisted Internal Test

Enable only after Stage 4 is stable.

Procedure:

1. Set `WEIGHT_REMINDERS_ENABLED=true`.
2. Add exactly one internal user to `WEIGHT_REMINDERS_ALLOWLIST`.
3. Recreate only `hermes-bot` according to the controlled rollout playbook.
4. Configure one near-future reminder occurrence.
5. Verify exactly one delivery.
6. Verify no duplicate after repeated scans.
7. Verify no duplicate after restart.
8. Press `Записать вес` and confirm existing pending flow is used.
9. Press disable and confirm future delivery stops.

## Stage 6 - Gradual Rollout

Expansion sequence:

- one internal user;
- small internal allowlist;
- expanded allowlist;
- global opt-in UI after explicit approval.

Global automatic opt-in is forbidden.

## Smoke Checklist

Functional:

- `/weight` opens weight screen;
- reminder settings open for allowlisted user;
- settings persist;
- due reminder sends exactly once;
- `Записать вес` enters pending weight input;
- valid weight creates one append-only history row;
- `История веса` opens history;
- disable prevents future sends.

Security:

- no Command Approval;
- no terminal/code/file/delegation tools;
- no generic Hermes dispatch;
- callbacks derive identity from Telegram context;
- forged callback rejected.

Privacy:

- no Telegram ID/chat ID/username;
- no raw callback data;
- no message text;
- no weight/profile/macro values;
- no raw timezone string;
- no raw exception body;
- no delivery key.

## Log Markers

Expected safe markers:

```text
[HealBite][weight_reminder_config]
[HealBite][weight_reminder_due]
[HealBite][weight_reminder_delivery]
[HealBite][weight_reminder_skip]
```

Allowed fields:

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

Forbidden fields:

```text
identifiers
callback payloads
message text
captions
weight values
profile values
macro values
raw timezone string
delivery key
SQL row payload
exception body
```

## Rollback

Primary switch:

```text
WEIGHT_REMINDERS_ENABLED=false
```

If runtime is unstable:

1. Disable the feature flag if config-only rollback is enough.
2. If needed, roll back only `hermes-bot`.
3. Do not recreate Qdrant.
4. Do not restore SQLite unless integrity is corrupt and restore is explicitly
   approved.
5. Do not drop reminder tables.

Rollback success criteria:

```text
hermes_running=true
restart_count=0
sqlite_integrity=ok
qdrant_unchanged=true
reminder_delivery_count_after_rollback=0
```

## Merge-Gate Operational Contract

The design review fixes these operational defaults:

```text
WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS=60
WEIGHT_REMINDER_CLAIM_LEASE_SECONDS=300
WEIGHT_REMINDER_BATCH_SIZE=50
WEIGHT_REMINDER_MISSED_GRACE_HOURS=12
```

Delivery state names expected in implementation:

```text
pending
claimed
sending
retry_wait
sent
delivery_unknown
permanent_failed
skipped
skipped_stale
```

Per-user permanent delivery failures must suspend delivery without clearing the
user's opt-in:

```text
blocked user / chat not found
-> delivery_state=suspended
-> enabled remains true
-> automatic deliveries stop
-> UI shows suspended state
```

Global bot auth failures must not mutate per-user settings:

```text
401/403 bot authentication failure
-> global scheduler circuit breaker
-> no per-user setting changes
-> safe critical marker only
```

Ambiguous delivery outcome:

```text
Telegram send may have been accepted
but sent_at_utc was not committed
-> status=delivery_unknown
-> no automatic retry for same occurrence
-> next scheduled occurrence continues normally
```

Before each send, implementation must verify:

```text
setting still enabled
delivery_state=active
schedule_version matches claimed occurrence
```

Mismatch means:

```text
status=skipped_stale
message not sent
next_due_at_utc recalculated
```

DST behavior:

```text
spring-forward nonexistent local time -> first valid instant after requested wall time
fall-back repeated local time -> first occurrence / fold=0
timezone change -> increment schedule_version and recalculate next_due_at_utc
```
