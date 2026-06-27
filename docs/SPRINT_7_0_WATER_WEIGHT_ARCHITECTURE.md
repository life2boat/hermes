# Sprint 7.0 Water, Weight, And Reminder Architecture

Status: design only. This document intentionally does not create tables, migrations, handlers, callbacks, FSM code, cron jobs, service classes, or production configuration.

## 0. Architecture Decision Log

These decisions finalize the Sprint 7.0B/7.0C design unless explicitly superseded before implementation:

| Area | Decision | Rationale |
| --- | --- | --- |
| Water table | `water_intake_events` event log with `user_id INTEGER`, `amount_ml INTEGER`, UTC timestamps, source, optional idempotency key, and partial unique idempotency index | Matches current SQLite source-of-truth model and avoids mutable daily counters |
| Water idempotency | Store `idempotency_key TEXT`; for Telegram callbacks use a deterministic key derived from callback/update context; partial unique index on `(user_id, idempotency_key)` | `telegram_update_id` is useful but not enough for all callback/edit flows; the service should accept a generic key |
| Water undo | Hard delete only the latest current-day row for the same user | Existing diary undo physically deletes rows; soft delete is unnecessary for MVP and complicates totals |
| Weight representation | Store historical measurements as `weight_grams INTEGER`; convert to/from profile `weight_kg REAL` at API boundaries | Avoids floating-point ambiguity in history while staying compatible with current profile store and calculator |
| Initial weight | The initial weight is the earliest valid row in `weight_measurements`; before Sprint 7.0C history exists, fallback to current profile `weight_kg` for display only | Avoids backfill while preserving current profile semantics |
| Current weight | Canonical current weight remains `profiles.weight_kg` | Current profile formatter and nutrition calculator already read this field |
| Atomic recalculation | Weight service should own a single SQLite transaction and call the pure nutrition calculator directly; do not call `HealBiteUserProfileStore.recalculate_profile_targets()` as an opaque nested transaction | Current profile store opens its own connection, so one-transaction safety needs a store helper or service-owned connection |
| Reminder semantics | First reminder is due 7 days after profile completion or latest valid measurement; each valid measurement pushes the next reminder 7 days out | User-specific and simpler than fixed weekday semantics |
| Reminder state | Add minimal SQLite `weight_reminder_state`; generic `cron/jobs.json` state is job-level, not per-user | Needed for restart-safe per-user idempotency and snooze state |
| Snooze/skip | MVP uses buttons `Записать вес` and `Не сейчас`; `Не сейчас` snoozes for 24 hours but does not create more than one reminder in the same due period | Keeps UX simple while preventing repeated spam after restarts |
| Timezone | No user timezone exists today; store UTC and use the current diary fallback: runtime local timezone via `datetime.now().astimezone()` for local-day boundaries | Matches current `/diary` behavior without inventing a profile field |
| Pending state | Add one tracker pending table/state namespace, not parallel ad-hoc FSMs; states: `water_custom_amount`, `weight_input` | Reuses the project pending-input pattern while keeping tracker text out of generic Hermes dispatch |
| Callback namespace | Use `water:*` and `weight:*` callback prefixes | Human-readable, short enough for Telegram, and distinct from existing `mp:`, `cl:`, `ea:`, `sc:` prefixes |
| Implementation split | Merge docs PR first; then PR 7.0B for water; then PR 7.0C for weight/reminders | Reduces blast radius and allows water UX to stabilize before reminder scheduling |

## 1. Scope

Sprint 7.0 is split into three parts:

- 7.0A: Cline legacy audit and migration plan.
- 7.0B: deterministic Water Tracker.
- 7.0C: weight measurements, macro recalculation, and weekly reminders.

This architecture assumes the Sprint 5.0 profile foundation is the canonical base.

## 2. Non-goals

Do not include:

- graphs
- weight loss forecasting
- reminders for every glass of water
- wearable integrations
- gamification
- AI recommendations for hydration
- medical advice
- Sprint 8.0 meal planning
- production deploy during design
- second source of truth outside SQLite
- generic Hermes agent/tool access for tracker flows

## 3. 7.0A Plan

7.0A produces:

- Cline legacy audit
- reuse/rewrite/reject matrix
- water and weight architecture
- test plan
- implementation sequence

No feature code is implemented in 7.0A.

## 4. 7.0B Water Tracker

### Product Behavior

User can:

- open the existing `💧 Трекер воды` rich keyboard entry
- see today's consumed water
- see daily target from canonical profile `water_target_ml`
- see remaining amount and completion percent
- add 250 ml
- add 500 ml
- enter custom amount
- undo last water entry for today
- refresh the screen
- return to main menu

The flow must be fully deterministic:

- no LLM
- no terminal/code/file tools
- no Command Approval
- no provider API

### Data Model

Prefer an event log, not a mutable daily counter:

```sql
CREATE TABLE IF NOT EXISTS water_intake_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount_ml INTEGER NOT NULL CHECK (amount_ml > 0),
    consumed_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'telegram',
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_water_intake_user_time
ON water_intake_events (user_id, consumed_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_water_intake_user_idempotency
ON water_intake_events (user_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

Notes:

- Use `INTEGER` user IDs to match current HealBite profile/diary code.
- Store timestamps in UTC as `%Y-%m-%d %H:%M:%S`.
- Compute "today" with local-day boundaries converted to UTC.
- Never physically reset counters at midnight.
- Exact totals come from SQLite `SUM(amount_ml)`.
- Do not index water records in Qdrant for MVP.

### Water Target Source

Only read from canonical profile:

- `profiles.water_target_ml`

Do not add a second water target source. If missing, show:

```text
Цель по воде ещё не настроена. Заполните /profile, чтобы я показывал прогресс.
```

### Service API

Suggested module:

- `gateway/healbite_water_tracker.py`

Suggested API:

```python
add_water_intake(user_id, amount_ml, consumed_at=None, source="telegram", telegram_update_id=None)
get_water_intake_today(user_id, now=None, timezone_name=None)
get_water_summary(user_id, now=None, timezone_name=None)
list_water_intake_today(user_id, now=None, timezone_name=None)
undo_last_water_intake_today(user_id, now=None, timezone_name=None)
parse_water_amount(text)
format_water_tracker_report(summary)
```

### Telegram Flow

Entry points:

- rich keyboard `💧 Трекер воды`
- optional future `/water`

Inline buttons:

```text
+250 мл
+500 мл
Другой объём
Отменить последнюю
Записать вес
Обновить
Назад
```

Suggested callback namespace:

- `hbw:add:250`
- `hbw:add:500`
- `hbw:custom`
- `hbw:undo`
- `hbw:weight`
- `hbw:refresh`
- `hbw:back`

Custom input FSM:

- create a small pending input state for water amount
- accept one text reply
- parse amount
- clear state on success or `/cancel`
- do not pass pending tracker text to generic Hermes lane

### Water Parser

Accept:

- `300`
- `300 мл`
- `0.5 л`
- `0,5 л`
- `стакан` as an optional later alias only if product approves a fixed ml value

Validation proposal:

- minimum one entry: 1 ml
- maximum one entry: 3000 ml
- liters converted to integer ml
- zero and negative values rejected
- non-numeric unknown text rejected locally

## 5. 7.0C Weight And Reminders

### Product Behavior

User can:

- open existing `⚖️ Трекер веса`
- enter current weight
- see previous and new weight
- save measurement history
- update canonical profile `weight_kg`
- recalculate calorie/protein/fat/carb targets using current calculator
- see updated targets after save

Weekly reminder:

- no more than one reminder per eligible interval
- no reminder for incomplete profile
- no LLM
- no provider API
- no Command Approval
- restart-safe

### Data Model

```sql
CREATE TABLE IF NOT EXISTS weight_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    weight_grams INTEGER NOT NULL CHECK (weight_grams > 0),
    measured_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'telegram',
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weight_measurements_user_time
ON weight_measurements (user_id, measured_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_weight_measurements_user_idempotency
ON weight_measurements (user_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

Reminder state is required because the existing scheduler stores job-level state in `cron/jobs.json`, not per-user due/snooze state:

```sql
CREATE TABLE IF NOT EXISTS weight_reminder_state (
    user_id INTEGER PRIMARY KEY,
    last_reminded_at TEXT,
    next_reminder_at TEXT,
    snoozed_until TEXT,
    updated_at TEXT NOT NULL
);
```

### Weight Parser

Accept:

- `82`
- `82.5`
- `82,5`
- `82,5 кг`

Validation:

- minimum: 35 kg
- maximum: 300 kg
- comma and dot accepted
- invalid input does not mutate profile or measurement history

### Macro Recalculation Contract

Current code note: `HealBiteUserProfileStore.recalculate_profile_targets()` opens its own connection. To keep the measurement insert, profile update, and target update atomic, Sprint 7.0C should either add a transaction-aware profile helper or let the weight service own one SQLite connection and call the pure `calculate_nutrition_targets()` function directly.

For valid weight entry:

1. Start SQLite transaction.
2. Insert `weight_measurements` row.
3. Update canonical profile `profiles.weight_kg`.
4. Recalculate targets with `calculate_nutrition_targets`.
5. Save calculated targets in `users`.
6. Commit.
7. Return previous weight, new weight, and updated targets.

If recalculation fails:

- rollback profile and measurement mutation if all steps are in one transaction; or
- use a carefully documented two-phase approach that keeps history but marks recalculation failure.

Recommendation: use one service-owned transaction for MVP; rollback the measurement/profile/target update together on validation or calculation failure.

### Service API

Suggested module:

- `gateway/healbite_weight_tracker.py`

Suggested API:

```python
record_weight_measurement(user_id, weight_grams, measured_at=None, source="telegram", idempotency_key=None)
get_latest_weight_measurement(user_id)
list_weight_measurements(user_id, limit=None)
get_weight_change(user_id)
apply_weight_to_profile_and_recalculate(user_id, weight_grams, measured_at=None, idempotency_key=None)
parse_weight_kg(text)
format_weight_update_report(result)
```

## 6. Reminder Scheduling

### Recommended Semantics

Use "7 days after profile completion or the last valid weight measurement" for MVP.

Reasons:

- user-specific and intuitive
- avoids fixed weekday/product preference
- naturally suppresses reminders after a recent measurement
- easier to make idempotent
- works with missing user timezone by using UTC plus the existing runtime-local fallback

### Scheduler Integration

Do not create a generic LLM cron prompt.

Acceptable implementation options:

1. Gateway-local deterministic periodic task that checks due users and sends via the live Telegram adapter.
2. Deterministic no-agent cron/script job only if the gateway-local path proves impractical.

Recommendation: prefer the gateway-local deterministic task for MVP so no shell/script execution is needed for user-facing reminders.

Either option must:

- keep state in SQLite
- mark reminder sent only after successful send
- skip incomplete profiles
- not run terminal/code tools
- not call LLM/provider APIs

### Reminder API

```python
should_send_weight_reminder(user_id, now=None)
mark_weight_reminder_sent(user_id, sent_at=None)
get_users_due_for_weight_reminder(now=None)
record_weight_reminder_decision(user_id, decision, now=None)
snooze_weight_reminder(user_id, until=None)
```

## 7. Timezone Semantics

Current diary code uses local server timezone via `datetime.now().astimezone()` for day windows. Sprint 7.0 should make timezone explicit.

Recommendation:

- Store all timestamps in UTC.
- Use profile timezone if a canonical field exists later.
- For Sprint 7.0 MVP, fallback to server/runtime timezone.
- Convert local day start/end to UTC for SQL queries.
- Do not store local-day counters.
- DST behavior: local midnight boundaries are computed with timezone-aware datetimes; ambiguous times are avoided by storing UTC.

Water "today":

- local calendar day from 00:00 inclusive to next 00:00 exclusive.

Weight reminder "week":

- 7 * 24 hours after last valid measurement for MVP.

## 8. Idempotency

Water and weight callbacks can be duplicated by Telegram retries or user double taps.

Recommended protections:

- store a generic `idempotency_key` when available; for Telegram callbacks derive it from the callback/update context
- unique partial index `(user_id, idempotency_key)`
- service returns duplicate/no-op result instead of raising
- undo only deletes the latest row for the current user and current local day
- never undo another user's row

## 9. Telegram Safety Boundary

Water/weight routes must stay in HealBite local lane:

- route before generic text dispatch
- block terminal/code/file/read tools
- no approval UI
- no LLM
- no provider raw errors
- PII-safe route markers only

Suggested markers:

- `healbite_route_selected route=water_tracker`
- `healbite_route_selected route=water_callback action=add_250`
- `healbite_route_selected route=water_input result=accepted|invalid|cancelled`
- `healbite_route_selected route=weight_tracker`
- `healbite_route_selected route=weight_input result=accepted|invalid|cancelled`
- `healbite_route_selected route=weight_reminder result=sent|skipped|duplicate`

Do not log raw user input, user IDs, chat IDs, usernames, or callback payloads containing PII.

## 10. Migration Plan

All migrations must be:

- lazy
- idempotent
- non-destructive
- SQLite-only
- tested with `tmp_path`

Suggested order:

1. Add water event table and indexes.
2. Add water service tests.
3. Wire Telegram water screen and callbacks.
4. Add water diagnostic CLI smoke.
5. Add weight measurement table and indexes.
6. Add weight service tests.
7. Wire Telegram weight input.
8. Add reminder state only after reminder design is approved.

## 11. Failure Handling

Water:

- invalid amount: local validation message
- missing target: show total and profile setup hint
- duplicate callback: show unchanged refreshed summary
- SQLite failure: safe generic Russian fallback, log sanitized error category

Weight:

- invalid weight: local validation message
- incomplete profile: ask user to complete `/profile`
- recalculation failure: do not partially mutate profile in MVP
- duplicate callback/update: return existing result or no-op

Reminder:

- failed send: do not mark as sent
- incomplete profile: skip silently or with diagnostic marker only
- repeated scheduler tick: no duplicate message

## 12. Test Strategy

### Water Storage

- idempotent migration
- add event
- daily sum
- user isolation
- day-boundary isolation
- timezone boundary
- validation
- undo latest today
- persistence across store instances
- duplicate update protection

### Weight Storage

- add measurement
- latest measurement
- history ordering
- user isolation
- validation
- persistence
- duplicate update protection
- profile updates only after valid measurement

### Macro Integration

- existing calculator reused
- new weight updates calorie target
- macro targets saved
- other profile data preserved
- no partial state on recalculation error
- previous measurements remain in history

### Reminder

- due user gets one reminder
- repeated scheduler run does not duplicate
- recent measurement delays reminder
- incomplete profile excluded
- restart-safe state
- no LLM/tool calls

### Telegram

- `💧 Трекер воды` opens water screen
- +250 and +500 add events
- custom input
- invalid input
- cancel
- undo
- `⚖️ Трекер веса` starts weight flow
- invalid weight
- profile recalculation result
- back/menu routing
- callback answer
- no generic Hermes dispatch
- no Command Approval

### Regression

- `/menu`
- `/profile`
- `/diary`
- `/stats`
- `/stats 7d`
- photo flow
- profile onboarding
- nutrition calculator
- update-command test isolation
- parallel test runner

## 13. Acceptance Criteria

### Water Tracker

- `💧 Трекер воды` opens a working local screen.
- 250/500 ml buttons add one event each.
- custom amount saves after validation.
- daily total is computed from SQLite.
- next local day shows 0 ml without deleting history.
- target comes only from profile `water_target_ml`.
- undo affects only latest current user's current-day event.

### Weight Check-in

- user can record a valid weight.
- measurement is saved in history.
- profile current weight updates.
- existing calculator recalculates targets.
- `/profile` shows updated weight and targets.
- weekly reminder sends no more than once per eligible interval.
- incomplete profiles are skipped.
- no other user's data is visible or mutable.

## 14. Rollback Compatibility

Because the proposed storage is additive:

- old production code ignores new tables.
- no existing nutrition/profile tables are dropped.
- no existing rows are rewritten.
- rollback image can run against DB with extra tables.

If weight recalculation adds no new columns and reuses Sprint 5.0 fields, rollback risk stays low.

## 15. Implementation Sequence

1. PR 7.0A: docs-only audit and architecture.
2. PR 7.0B-1: water store/service and tests.
3. PR 7.0B-2: Telegram water UI and CLI smoke.
4. PR 7.0C-1: weight store/service and parser tests.
5. PR 7.0C-2: profile update and macro recalculation integration.
6. PR 7.0C-3: deterministic reminder state and scheduler integration.
7. Controlled deploy only after green CI, headless diagnostics, DB backup, and manual Telegram smoke plan.

## 16. Required Test Files

Recommended test layout for implementation PRs:

| File | Purpose |
| --- | --- |
| `tests/gateway/test_healbite_water_store.py` | water schema, event writes, sums, undo, idempotency, user isolation, day boundary |
| `tests/gateway/test_healbite_water_service.py` | parser, validation messages, target loading, report formatting, no production DB path |
| `tests/gateway/test_telegram_water_tracker.py` | rich keyboard route, callbacks, custom input FSM, cancel/back, no generic dispatch/tools |
| `tests/gateway/test_healbite_weight_store.py` | measurement schema, integer grams, latest/history, idempotency, user isolation |
| `tests/gateway/test_healbite_weight_service.py` | parser, validation, atomic profile update, macro recalculation, rollback on failure |
| `tests/gateway/test_telegram_weight_tracker.py` | weight entry route, invalid input, confirmation/report, no generic dispatch/tools |
| `tests/cron/test_healbite_weight_reminders.py` | due selection, snooze, skip, restart-safe state, incomplete profile exclusion, duplicate prevention |
| `tests/scripts/test_healbite_cli.py` additions | `test-water`, `test-weight`, `test-reminder` diagnostic smoke commands |

Regression tests must also cover `/menu`, `/profile`, `/diary`, `/stats`, `/stats 7d`, photo flow, profile onboarding, current nutrition target calculation, update-command isolation, and the parallel test runner.

## 17. Acceptance Criteria Checklist

### Water Tracker

- Placeholder `💧 Трекер воды` opens a real local screen.
- Quick-add 250/500 ml persists one event per action.
- Custom input accepts ml and liter forms.
- Invalid input writes nothing.
- SQLite persists all water events.
- Totals are isolated by user.
- Today's total uses the local-day window.
- History is not deleted at midnight.
- Undo removes only the current user's latest event for the current local day.
- Duplicate callback/update does not create a duplicate event.
- `water_target_ml` is read only from the profile store.
- Flow does not use LLM/provider APIs or general tools.

### Weight And Reminders

- Measurement history is stored as integer grams.
- Current profile weight remains `profiles.weight_kg`.
- Initial weight is derived from earliest measurement, or profile weight until history exists.
- Parser accepts dot and comma decimal input.
- Valid input updates profile and targets.
- Existing nutrition calculator is reused.
- Transaction/failure safety prevents partial updates.
- Duplicate Telegram update/callback is idempotent.
- Reminder state is restart-safe.
- Incomplete profiles are excluded.
- User data is never leaked in logs or cross-user results.

## 18. Implementation PR Split

1. Merge Draft PR #10 after design approval.
2. Create implementation PR 7.0B for Water Tracker only.
3. After 7.0B is merged and smoke-tested, create implementation PR 7.0C for weight history, macro recalculation, and reminders.
4. Do not combine reminders with water in the same implementation PR unless explicitly approved.
