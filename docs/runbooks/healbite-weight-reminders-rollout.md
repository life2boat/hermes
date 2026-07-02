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


## Canonical Production DB Audit

Use the repository audit helper before any beta enablement or production
reminder validation:

```bash
python scripts/weight_reminder_db_audit.py /home/hermes/healbite.db --format json
```

Canonical reminder schema names are:

```text
settings table=weight_reminder_settings
deliveries table=weight_reminder_deliveries
```

Audit tooling must import the shared schema constants from
`gateway.healbite_weight_reminder_schema` or use `scripts/weight_reminder_db_audit.py`.
Do not hardcode historical ad-hoc names such as `weight_reminder_outbox` or
`weight_reminder_delivery_attempts` in production validation logic; those names
are non-canonical and must fail as `unexpected_reminder_schema`, not as a clean
`not_initialized` result.

Production DB audit rules:

- open SQLite with `mode=ro`;
- execute `PRAGMA query_only=ON`;
- run `PRAGMA integrity_check`;
- inspect `sqlite_master` and aggregate counts only;
- never run the initializer against production as part of an audit;
- never create missing tables during audit;
- never output user IDs, delivery keys, raw rows, Telegram identifiers, timezone
  strings, weight values, profile values, or exception bodies.

`schema_state=not_initialized` is valid only when no `weight_reminder_%` tables
exist. A database with only one canonical table is `partial_canonical` and must
fail audit. Unknown reminder tables are not equivalent to `not_initialized`.
After a controlled allowlisted rollout, nonzero disabled settings and sent
delivery counts are expected append-only history and must not be deleted to get a
clean baseline.

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


## PR4 Observability Hardening

Controlled allowlisted rollout evidence from
`/home/hermes/backups/s70c2_allowlisted_rollout/20260702T131809Z`
confirmed one internal user, one reminder setting, one logical occurrence, one
sent delivery row, no retries, no delivery_unknown, no permanent failures, no
duplicate before or after a `hermes-bot` recreate, append-only history
preserved, feature disabled afterward, allowlist empty afterward, SQLite
integrity ok, and Qdrant unchanged.

Root cause of the observability gap: authoritative SQLite outbox state confirmed
successful delivery, but scheduler/delivery markers were not complete enough for
post-smoke validation. In particular, disabled-start markers could be confused
with lifecycle starts, due/claim/delivery transitions were not consistently
expressed as separate safe events, and validation methodology needed to avoid
using correlation IDs as delivery counts.

Safe marker families:

```text
[HealBite][weight_reminder_scheduler]
[HealBite][weight_reminder_due]
[HealBite][weight_reminder_delivery]
[HealBite][weight_reminder_skip]
[HealBite][weight_reminder_config]
```

Safe fields only:

```text
route
action
outcome
delivery_state
delivery_attempted
delivery_completed
retry_scheduled
permanent_failure
ambiguous_delivery
claim_recovered
stale_schedule
enabled
timezone_present
timezone_region_bucket
weekday_bucket
time_bucket
attempt_count_bucket
batch_size_bucket
due_count_bucket
error_type
corr_present
corr
```

Forbidden in every sink:

```text
user IDs
Telegram IDs
chat IDs
allowlist values
delivery keys
raw timezones
exact local times
scheduled or next-due timestamps tied to a user
message text
callback payloads
draft contents
weight, history, profile, or macro values
raw exception bodies
Telegram or provider response bodies
```

Authoritative state ordering:

- `outcome=sent` may be logged only after Telegram send success, outbox
  `status=sent`, `last_delivered_at`, and the next due schedule are committed.
- `outcome=retry_scheduled` may be logged only after retry state and next
  attempt are committed.
- `outcome=delivery_unknown` may be logged only after the ambiguous outcome is
  committed and the occurrence is advanced without retry.
- `outcome=permanent_failed` may be logged only after the outbox permanent
  failure and any setting suspension are committed.
- `outcome=skipped` or `outcome=skipped_stale` may be logged only after the
  skip state is committed.

Logging is best-effort. Marker emission must not change DB transactions, retry
policy, Telegram send behavior, outbox state, polling health, or scheduler
lifecycle.

### Cross-Sink Logical Event Counting

A logical event must not be counted by raw physical log lines or by unique
correlation IDs. A single `LogRecord` may appear in more than one sink, and two
separate events may intentionally share correlation presence.

Use this normalized safe signature:

```text
marker family
action
outcome
error_type
attempt bucket
correlation presence
bounded timestamp window
```

For each signature, count occurrences per sink and take the maximum count across
sinks. Total logical count is the sum of those per-signature maxima.

This avoids double-counting one event duplicated into `agent.log` and
`gateway.log`, and avoids collapsing distinct events that share the same
correlation value.

## Limited Beta Rollout

### Beta Cohort

- Only explicitly opted-in users are eligible.
- Initial cohort: 3-5 users.
- Each user must be added to the dedicated reminder allowlist.
- The admin allowlist is not a reminder cohort.
- No automatic opt-in.
- Do not store user IDs in reports, filenames, manifests, or Git.

### Entry Requirements

```text
PR4 merged
safe markers verified in isolated tests
feature disabled before rollout window
production DB backup completed
DB integrity=ok
current production image exact
Qdrant healthy
```

### Rollout Sequence

```text
add only approved cohort
feature=true
allowlist count equals cohort size
users configure reminders themselves through UI
operators do not create settings on behalf of users
observe for at least 7 days
```

### Daily Monitoring

Collect only PII-safe aggregates:

```text
active reminder settings count
sent occurrences count
retry_wait count
delivery_unknown count
permanent_failed count
duplicate occurrence violations
scheduler circuit-breaker events
container restart count
DB integrity
privacy-canary result
```

Do not preserve lists of Telegram IDs or user IDs in daily reports.

### Stop Thresholds

Immediately return `WEIGHT_REMINDERS_ENABLED=false` if any of these occur:

```text
any duplicate send
delivery_unknown before review
privacy leak
DB integrity failure
scheduler circuit breaker
unexpected container restart
allowlist mismatch
reminder UI visible to non-allowlisted users
retry storm
```

### Beta Completion Criteria

```text
at least 7 days
at least 3 explicitly opted-in users
0 duplicates
0 privacy leaks
0 unresolved delivery_unknown cases
no retry storms
stable DB integrity
```

### Exit

After beta, make a separate decision for cohort expansion or global opt-in UI.
An empty allowlist must never mean global rollout.
