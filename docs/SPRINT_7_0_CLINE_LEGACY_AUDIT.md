# Sprint 7.0A Cline Legacy Audit

Status: design-only audit. No feature code, migrations, scheduler changes, production DB changes, deploy, merge, or canonical checkout edits were performed.

## 1. Sources Inspected

- Clean Sprint worktree: `/home/hermes/.hermes/worktrees/healbite-water-weight-s70`
- Base main: `ab205857a99549a35b0f47b95a367d121218f2c1`
- Canonical checkout, read-only only: `/home/hermes/.hermes/hermes-agent`
- Other worktrees, read-only file-name inventory only: `/home/hermes/.hermes/worktrees`
- Backup file-name inventory only: `/home/hermes/backups`
- Git refs and history, read-only:
  - local branches
  - remote branches
  - tags
  - reflog snippets
  - `git log --all` grep for water/weight terms

No `.env`, token, API key, image payload, SQLite row contents, private memory capsule contents, or user PII was printed or copied.

## 2. Current Main Inventory

### Telegram Menu And Routing

Current HealBite rich keyboard is defined in `gateway/platforms/telegram.py`:

- `HEALBITE_REPLY_KEYBOARD_ROWS`
- `HEALBITE_REPLY_KEYBOARD_ACTIONS`
- `_healbite_reply_keyboard`
- `_healbite_main_menu_keyboard`
- `_healbite_command_from_text`
- `_dispatch_healbite_keyboard_action`
- `_maybe_handle_healbite_menu_button`
- `_maybe_reject_healbite_compound_input`

Current placeholder mappings:

- `⚖️ Трекер веса` -> `__placeholder__:weight_tracker`
- `💧 Трекер воды` -> `__placeholder__:water_tracker`

Both placeholders currently return the shared local response:

- `HEALBITE_PLACEHOLDER_REPLY = "В разработке"`

No `/water` command or `/weight` command exists in current main.

### Public And Privileged Lanes

Current Telegram adapter already separates local HealBite routes from generic Hermes dispatch:

- `/start`
- `/profile`
- `/diary`
- `/stats`
- `/undo_meal`
- rich keyboard actions
- multiline local input rejection
- public onboarding block reasons
- generic lane handoff logged at DEBUG

The marker helper `_log_healbite_route_selected` is PII-safe by design: it logs a correlation hash and allowlisted route/action/lane/result fields.

### Current Profile Store

Canonical profile code lives in `gateway/healbite_user_profile.py`:

- `HealBiteUserProfileStore`
- `HealBiteUserProfile`
- `HealBiteOnboardingState`
- `get_default_healbite_user_profile`
- `get_existing_healbite_user_profile`

Tables managed by the profile store:

- `users`
- `profiles`
- `user_onboarding_state`

The profile store supports:

- sex
- age
- height
- current weight (`weight_kg`)
- goal
- activity level
- optional manual calorie target
- calculated calorie/protein/fat/carb targets
- calculation version metadata
- `water_target_ml` column on `profiles`

Schema migration pattern:

- `CREATE TABLE IF NOT EXISTS`
- guarded `ALTER TABLE ... ADD COLUMN`
- no destructive migration

Compatibility logic exists for legacy identity naming:

- `users.user_id`
- `users.telegram_id`
- `profiles.telegram_id`
- `profiles.user_id`

### Nutrition Target Calculator

Calculator code lives in `gateway/healbite_nutrition_targets.py`:

- `NutritionProfileInputs`
- `calculate_nutrition_targets`
- `NutritionTargetCalculation`
- `validate_profile_inputs`

Current calculator:

- Mifflin-style BMR
- activity factor
- goal factor
- protein grams from weight
- fat grams from weight
- carbs from remaining calories
- input validation for age, height, weight, sex, activity, goal

Weight bounds currently enforced:

- minimum weight: 35 kg
- maximum weight: 300 kg

### Nutrition Diary Store As Pattern

The existing diary store in `gateway/healbite_nutrition_diary.py` is the strongest local model for Sprint 7.0 store design:

- SQLite source of truth
- lazy schema init
- `HEALBITE_DB_PATH` override via `resolve_healbite_db_path`
- optional best-effort Qdrant indexing for diary records only
- user isolation on all writes/reads
- day-window helper `_local_day_window`
- UTC timestamp storage via `_sqlite_timestamp`
- singleton lifecycle for production, explicit `db_path` for tests
- pending confirmation table for photo nutrition

### Current Water Code

Current main has no active Water Tracker implementation.

Existing related pieces:

- placeholder keyboard label: `💧 Трекер воды`
- profile column: `profiles.water_target_ml`
- no water service
- no water parser
- no water callbacks
- no water FSM
- no water-specific tests

### Current Weight Code

Current main has no active standalone weight measurement tracker.

Existing related pieces:

- placeholder keyboard label: `⚖️ Трекер веса`
- profile field: `profiles.weight_kg`
- onboarding weight step
- nutrition calculator uses profile weight
- no weight history service
- no weekly reminder service
- no weight-specific callbacks
- no weight-specific FSM tests

### Current Scheduler

Scheduler code is generic Hermes cron:

- `cron/jobs.py`
- `cron/scheduler.py`

Properties:

- jobs are stored under Hermes home as `cron/jobs.json`
- scheduler runs due jobs from gateway tick
- supports next/last run state
- supports delivery to Telegram and other platforms
- supports no-agent/script style jobs
- also supports LLM/agent jobs, which must not be used for weight reminders

Conclusion: Sprint 7.0 reminders should not use generic LLM cron prompts. If cron is reused, use deterministic no-agent/script flow or a dedicated local reminder service with explicit idempotency state.

## 3. Legacy Cline Inventory

No confirmed Cline water/weight implementation was found.

Read-only searches covered:

- `/home/hermes/.hermes/hermes-agent`
- `/home/hermes/.hermes/worktrees`
- `/home/hermes/backups`
- local branches
- remote branches
- tags
- reflog
- git history

Findings:

- Remote `origin/cline-port/*` branches exist, but their names point to unrelated upstream agent work:
  - cache token handling
  - gateway memory monitor
  - nested tool arg coercion
  - OpenRouter cache control
  - parallel tool-call guidance
  - browser URL plugin install
- No branch, tag, reflog entry, or history entry showed a HealBite water tracker, hydration tracker, weight tracker, or measurement tracker implementation.
- File-name searches mostly matched unrelated terms:
  - `watermark`
  - `weights-and-biases`
  - generic "hydration" in non-HealBite contexts
- Backup search did not reveal water/weight files.
- Canonical dirty diff did not contain water/weight/measurement HealBite changes.

## 4. Provenance

| Artifact | Source | Tracked | Cline-related | Purpose | Transfer status |
| --- | --- | --- | --- | --- | --- |
| `gateway/platforms/telegram.py` placeholders | current main | yes | no | rich keyboard, local routes | adapt current main |
| `gateway/healbite_user_profile.py` | current main | yes | no | canonical profile store, onboarding, weight field | reuse/adapt |
| `gateway/healbite_nutrition_targets.py` | current main | yes | no | KБЖУ calculator | reuse |
| `gateway/healbite_nutrition_diary.py` | current main | yes | no | SQLite store pattern, day windows | adapt patterns |
| `cron/jobs.py`, `cron/scheduler.py` | current main | yes | no | generic scheduler | adapt carefully or avoid |
| `origin/cline-port/*` branches | upstream remote refs | yes remote refs | yes by name, not HealBite | unrelated agent work | reject for Sprint 7.0 |
| config `.bak` files under Hermes home | local backups | no | unconfirmed | config backups, not feature code | reject for code transfer |

## 5. Security Findings

- No confirmed legacy water/weight code is safe to copy.
- No legacy code should be copied automatically from dirty canonical checkout.
- Generic cron agent routes are too broad for weekly weight reminders because they can involve LLM/tool execution. Sprint 7.0 reminders must be deterministic.
- Telegram placeholder routes are currently local and do not invoke tools; this safety property must be preserved.
- Existing route markers are PII-safe and should be extended rather than replaced.
- Current tests include some numeric ID fixtures. These are test fixtures and redaction checks, not production routing constants. Do not add hardcoded user/admin IDs to Sprint 7.0 code.
- `gateway/healbite_nutrition_diary.py` has a production default DB path, but tests mostly override it with `tmp_path` or `HEALBITE_DB_PATH`; Sprint 7.0 tests must follow this pattern.

## 6. Hardcoded IDs

No production HealBite water/weight code exists and no production hardcoded IDs were found for this sprint.

Known ID-like values appear in tests and diagnostic fixtures to verify policy, admin handling, user isolation, and redaction. Sprint 7.0 must not introduce any real Telegram ID in production code or docs.

## 7. Database Findings

Current main source of truth pattern:

- SQLite first
- `HEALBITE_DB_PATH` override for tests
- default path via `resolve_healbite_db_path`
- no Qdrant for exact totals
- Qdrant optional only for semantic diary indexing

Current profile DB foundation:

- `users` stores macro target values and calculation metadata
- `profiles` stores current user profile values, including `weight_kg` and `water_target_ml`
- `user_onboarding_state` stores profile onboarding FSM state

Missing:

- water event table
- weight measurement table
- reminder state table or equivalent state model
- water/weight-specific idempotency keys for callback/update dedupe

## 8. FSM Findings

Existing deterministic FSM/pending patterns:

- profile onboarding in `user_onboarding_state`
- pending meal confirmation in `pending_meals`
- Telegram multiline guard before local routing
- local keyboard actions short-circuit before generic Hermes lane

Missing for Sprint 7.0:

- water custom amount input state
- weight input state
- cancellation routing for these states
- "back" routing from tracker screen
- duplicate callback/update protection for water/weight buttons

## 9. Keyboard And Routing Findings

Current rich keyboard already contains required entry points:

- `💧 Трекер воды`
- `⚖️ Трекер веса`

They are placeholders, not inline callbacks. Sprint 7.0 can reuse these labels as local route triggers.

Current inline callbacks in Telegram adapter are used for unrelated generic features:

- model picker
- clarify
- approval
- command confirmations

Recommendation: Sprint 7.0 should not piggyback on generic approval callbacks. Use a small HealBite-specific callback namespace, for example:

- `hbw:add:250`
- `hbw:add:500`
- `hbw:custom`
- `hbw:undo`
- `hbw:refresh`
- `hbw:back`
- `hbw:weight`

## 10. Test Coverage

Strong existing coverage:

- profile onboarding
- profile target persistence
- macro target calculation
- diary summary
- diary target loading
- pending meal yes/no
- photo flow safety
- public onboarding access control
- route marker PII-safety
- diagnostic CLI smoke for profile/diary/correction/pending

Missing coverage for Sprint 7.0:

- water schema migration
- water add/sum/undo
- water parser
- water callback duplicate protection
- water day boundary
- water user isolation
- water custom input FSM
- weight measurement history
- weight parser
- weight profile update transaction
- target recalculation after weight change
- weekly reminder idempotency
- incomplete profile exclusion
- no LLM/tool execution for water/weight flows

## 11. Reuse Matrix

| Component | Current main | Legacy Cline | Decision | Reason |
| --- | --- | --- | --- | --- |
| Water parser | missing | not found | Rewrite | Need deterministic parser with tests |
| Water DB store | missing | not found | Rewrite | SQLite event model required |
| Water callbacks | placeholder only | not found | Adapt current router | Existing local keyboard lane is safe |
| Water FSM | missing | not found | Rewrite | Needs explicit input state and cancel |
| Weight parser | onboarding numeric parse only | not found | Adapt + harden | Existing `_to_float` handles comma/dot but not full tracker semantics |
| Weight history | missing | not found | Rewrite | Need event table and history |
| Macro recalculation | exists | not found | Reuse | Existing calculator and profile store are canonical |
| Scheduler | generic cron exists | not found | Adapt carefully | Use deterministic/no-agent path only |
| Reminder state | missing | not found | Rewrite minimal | Need restart-safe idempotency |
| Tests | profile/diary strong, tracker missing | not found | Add new tests | Must cover state isolation and no tools |

## 12. Rejected Legacy Components

- Any `origin/cline-port/*` branch code: not HealBite water/weight and not part of the product sprint.
- Any config `.bak` file: may contain environment/config history, not feature code.
- Any untracked/dirty canonical file not explicitly tied to water/weight: unsafe and unrelated.
- Any generic cron LLM job for reminders: violates "no provider/no tools/no Command Approval" requirement.

## 13. Open Questions

1. Should Water Tracker use inline keyboard buttons or a reply-keyboard screen? Recommendation: inline keyboard for tracker actions, keep rich reply keyboard as entry point.
2. Should daily water target default when `water_target_ml` is empty? Recommendation: display "goal not configured" and optionally a conservative UX hint, but do not create a second target source.
3. Should weight reminder be exactly 7 days after last measurement or a fixed weekday? Recommendation: 7 days after last valid measurement for MVP because it is user-specific and simpler to dedupe.
4. Should "remind later" be in MVP? Recommendation: exclude from first implementation unless reminder state model includes snooze semantics.
5. Should weight update immediately recalculate targets or ask confirmation? Recommendation: record measurement and show recalculated targets in one deterministic confirmation response; do not use LLM.

## 14. Recommended Migration Path

1. Implement Water Tracker first as a deterministic SQLite event store.
2. Wire the existing `💧 Трекер воды` placeholder to a local water screen.
3. Add custom water amount FSM and `/cancel` handling.
4. Implement weight measurement store and parser.
5. Wire `⚖️ Трекер веса` to weight input flow.
6. Reuse `HealBiteUserProfileStore.recalculate_profile_targets` after valid weight changes.
7. Add deterministic weekly reminder state only after store/service tests pass.
8. Add diagnostic CLI commands: `test-water`, `test-weight`, `test-reminder`.
9. Deploy only after isolated tests, headless diagnostics, and manual Telegram smoke.
## 15. Confirmed Audit Outcome

The following audit conclusions are now treated as the source of truth for Sprint 7.0 design:

- Current `main` has only placeholder buttons for water and weight.
- There is no full Water Tracker in current `main`.
- There is no weight measurement history or reminder flow in current `main`.
- No confirmed HealBite-specific Cline legacy implementation for water or weight was found.
- `origin/cline-port/*` does not apply to HealBite Water/Weight.
- Backup/config files must not be transferred automatically.
- Reuse is limited to the current profile store, nutrition target calculator, Telegram local routing, keyboard patterns, pending-input patterns, SQLite isolation via `HEALBITE_DB_PATH`, scheduler infrastructure, PII-safe logging, and CLI smoke patterns.

Anything else remains unconfirmed legacy and must not be copied into implementation branches without a separate provenance review.
