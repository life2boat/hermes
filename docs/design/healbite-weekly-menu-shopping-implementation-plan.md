# HealBite Weekly Menu and Shopping Implementation Plan

Status: design-only plan

Base architecture:

- ADR-0071 household-aware weekly menu and shopping domain
- ADR-0072 household foundation bootstrap
- ADR-0073 household weekly menu and shopping domain

## Current Baseline

Confirmed baseline at design lock:

- production household foundation already exists and is canonical;
- household feature remains disabled;
- household allowlist remains empty;
- Telegram placeholders for weekly menu, shopping list, and family stay unchanged;
- no weekly menu or shopping business rows exist yet;
- no startup path may create menu or shopping aggregates implicitly.

Confirmed post-C5A merge baseline:

- C5A merged in `main` at `31f2594d2de352db3c0c6c78513770bdf5c606ab`;
- production remains on old revision `04566a0dd2b79f60748194cc3d318c5a5e75f3d3`;
- production weekly-menu schema remains uninitialized;
- production shopping schema remains uninitialized;
- Telegram mutation UI is still undeployed;
- feature enablement remains a separate future canary stage.

## Design Rules That All Implementation PRs Must Preserve

1. Weekly menu is household-scoped, not member-scoped.
2. Shopping list is household-scoped, but separate from weekly menu.
3. Profile, nutrition targets, diary, weight, water, and reports remain canonical in existing tables.
4. Every mutation must require authorization context, expected version, and idempotency semantics.
5. Feature flags default disabled and allowlists default empty.
6. No implementation stage may auto-enable existing production households.
7. No startup path may create menu or shopping aggregates.
8. Production rollout always uses exact-image deployment and additive schema only.

## PR Sequence

### C1 - Weekly-Menu Schema and Store Core

Goal:

- introduce additive weekly-menu schema and store primitives only.

Expected files:

- `gateway/healbite_weekly_menu_schema.py`
- `gateway/healbite_weekly_menus.py`
- weekly-menu schema and store tests
- audit and contract tests updated as needed

Schema changes:

- add `household_weekly_menus`
- add `household_weekly_menu_entries`
- store one immutable menu revision snapshot per menu row
- add revision-ordering and partial-uniqueness constraints for one draft and one published revision per logical series
- add indexes and checks for UUIDs, week_start, status, version, ordering

Runtime changes:

- none in Telegram routing
- no startup writes
- no lazy aggregate creation from user traffic

Tests:

- schema idempotency
- UUID validation
- week-start validation
- status and version validation
- revision-number monotonicity and uniqueness
- single-draft and single-published partial uniqueness
- household isolation
- atomic draft replacement contract at store level

Feature state:

- disabled
- allowlist empty

Production impact:

- exact-image deploy with feature disabled
- additive schema initialization only
- no business rows created

Rollback:

- image rollback if startup regressions occur
- schema remains additive; no business-row cleanup required

Stop conditions:

- schema not canonical
- startup writes detected
- foreign-key or orphan violations
- runtime permission gate failure

### C2 - Shopping Schema and Store Core

Goal:

- introduce additive shopping schema and deterministic item and store primitives.

Expected files:

- `gateway/healbite_shopping_schema.py`
- `gateway/healbite_shopping.py`
- shopping schema and store tests

Schema changes:

- add `household_shopping_lists`
- add `household_shopping_items`
- add `household_shopping_idempotency`
- add uniqueness, version, status, and ordering constraints

Runtime changes:

- none in Telegram routing
- no automatic list derivation at startup
- no household bootstrap side effects

Implemented public surface:

- `gateway/healbite_shopping_schema.py`
  - `initialize_shopping_schema(...)`
  - `detect_shopping_schema_state(...)`
  - quantity/unit normalization helpers and enum contracts
- `gateway/healbite_shopping.py`
  - `HealBiteShoppingStore.initialize_schema()`
  - `HealBiteShoppingStore.audit_schema()`
  - `create_shopping_list(...)`, `get_shopping_list(...)`, `list_shopping_lists(...)`
  - `add_manual_item(...)`, `update_item(...)`, `set_item_checked(...)`
  - `replace_or_regenerate_generated_items(...)`
  - `activate_shopping_list(...)`, `complete_shopping_list(...)`, `archive_shopping_list(...)`

Deterministic contracts locked in C2:

- standalone shopping lists are allowed with `source_menu_id = NULL`
- menu-derived lists retain exact immutable `source_menu_id` linkage
- generated-item regeneration preserves existing item identity for matched rows
- checked unmatched generated rows are promoted to manualized overrides instead of being dropped
- deduplication is fingerprint-based and refuses ambiguous `unknown`-unit merges
- no Telegram or runtime bridge is introduced before C3

Tests:

- item quantity representation
- quantity precision, scale, and decimal normalization
- unit compatibility refusal
- manual item persistence model
- list lifecycle and foreign-key safety
- exact immutable menu-revision linkage
- household isolation and optimistic concurrency

Feature state:

- disabled
- allowlist empty

Production impact:

- additive schema only
- no menu or list business rows created

Rollback:

- image rollback only
- additive schema retained

Stop conditions:

- incompatible unit merge behavior
- orphan shopping rows
- implicit business-row creation

### C3 - Feature-Disabled Runtime Services

Goal:

- wire read-only service boundaries for weekly menu and shopping with strict feature gates.

Expected files:

- runtime resolver and service modules
- gate helpers in runtime boundary
- audit tests for disabled behavior

Schema changes:

- none

Runtime changes:

- internal services only
- no Telegram UI changes yet
- no callbacks yet
- fail-closed feature gates

Implemented runtime surface:

- `gateway/healbite_feature_gates.py`
  - immutable feature-gate config
  - strict boolean and allowlist parsing
  - fail-closed malformed-config behavior
  - canonical actor normalization for application user IDs only
- `gateway/healbite_weekly_menu_runtime.py`
  - `get_availability(actor_id)`
  - `get_weekly_menu_for_week(actor_id, week_start)`
  - `get_weekly_menu_revision(actor_id, revision_id)`
  - `list_weekly_menu_revisions(actor_id, week_start)`
- `gateway/healbite_shopping_runtime.py`
  - `get_availability(actor_id)`
  - `get_shopping_list(actor_id, shopping_list_id)`
  - `list_shopping_lists(actor_id, filters)`
  - `list_shopping_items(actor_id, shopping_list_id)`

Locked C3 invariants:

- menu and shopping feature gates are independent from household bootstrap enablement;
- malformed boolean or allowlist config disables the feature and marks configuration invalid;
- empty allowlists deny access without opening menu or shopping stores;
- feature gate order is config -> enabled -> canonical actor -> allowlist -> household auth -> runtime store -> schema state -> read;
- runtime store factories return explicit resource contexts rather than bare stores;
- owned runtime resources always finalize on every exit path and roll back any open transaction before close;
- borrowed runtime resources are never closed, committed, or rolled back by the runtime;
- runtime never caches live store resources between calls and treats the default C1/C2 stores as stateless method-scoped adapters only;
- cleanup failure is fail-closed and never returns a normal success result;
- C3 runtime never mutates menu, shopping, diary, profile, or household business rows;
- C3 runtime returns safe availability states only and does not expose DB paths, raw allowlists, or raw exception text.

Tests:

- disabled feature returns no-op or not-available
- allowlist empty blocks access
- no startup aggregate creation
- authorization context required for every store call

Feature state:

- disabled
- allowlist empty

Production impact:

- exact-image deploy feature-disabled
- runtime health and household rows audited

Rollback:

- image rollback

Stop conditions:

- service path creates business rows on read
- disabled state leaks partial UI or callbacks

### C4 - Telegram Read-Only Menu UI Behind Allowlist

Goal:

- expose read-only weekly menu entry point behind a dedicated allowlist.

Expected files:

- Telegram UI bridge for weekly menu only
- read-only renderers and builders
- observability markers for route selection

Schema changes:

- none

Runtime changes:

- `📋 Меню на неделю` may open a read-only screen behind allowlist
- `🛒 Список покупок` and `👨‍👩‍👧‍👦 Семья` still stay `В разработке`
- no callbacks or inline keyboards are introduced in C4
- one Telegram click performs one weekly runtime read flow
- only the active published revision is visible
- draft-only and archived-only states resolve to the same safe empty-state
- feature-disabled, malformed, invalid-actor, and not-allowlisted states keep returning `В разработке`
- household or schema unavailability returns `Функция временно недоступна. Попробуйте позже.`
- current week uses Monday `week_start` with documented fallback timezone `UTC`
- no startup DB open and no schema initialization happen at import or handler registration time

Tests:

- placeholder unchanged when feature disabled
- allowlisted read-only rendering
- safe formatter and chunker coverage
- no draft or archived leakage
- parallel actor isolation
- import-time side-effect guard
- no business-row creation from reads

Feature state:

- disabled by default
- narrow allowlist canary only

Production impact:

- read-only canary
- no mutations allowed

Rollback:

- feature disable or image rollback

Stop conditions:

- non-allowlisted access works
- callback payload trusts raw IDs
- read-only UI mutates DB

### C5 - Controlled Menu Mutations and Generation

Goal:

- enable draft creation, draft replacement, publish, archive, and optional LLM-backed generation.

Expected files:

- mutation services
- generation validator boundary
- publish and archive APIs
- menu concurrency and idempotency tests

Schema changes:

- none, unless a narrowly justified metadata column is missing

Runtime changes:

- controlled mutation handlers behind weekly-menu allowlist
- explicit publish flow
- explicit regenerate flow

Implemented C5A surface:

- `gateway/healbite_weekly_menu_mutation_runtime.py`
  - `HealBiteWeeklyMenuMutationRuntimeService`
  - `create_draft_for_week(...)`
  - `replace_draft_entries(...)`
  - `publish_draft(...)`
  - `archive_revision(...)`
- `gateway/healbite_weekly_menu_generation.py`
  - `CanonicalWeeklyMenuMemberSnapshotProvider`
  - `AuxiliaryWeeklyMenuGenerator`
  - `HealBiteWeeklyMenuGenerationService.generate_draft_for_week(...)`
  - strict structured-output parser and typed generation statuses
- `gateway/healbite_weekly_menu_generation_types.py`
  - request, member snapshot, generated entry, and response contracts
- `gateway/healbite_weekly_menus.py`
  - `lookup_generated_draft_replay(...)`
  - `apply_generated_draft_entries(...)`
- `gateway/healbite_user_profile.py`
  - read-only `HealBiteWeeklyMenuProfileSnapshot`
  - `get_weekly_menu_profile_snapshot(...)`

Locked C5A invariants:

- runtime mutations are owner-only even if lower store layers still permit broader household roles;
- the existing weekly-menu feature flag and allowlist remain the only entry gate and stay fail-closed;
- generation uses a small canonical read-only member/profile snapshot adapter and does not introduce ad-hoc SQL outside store boundaries;
- no DB transaction remains open during provider execution;
- generation writes are atomic at the final draft-write step only and never auto-publish;
- existing published revisions remain immutable when a new generated draft is created;
- replay is durable through the existing weekly-menu idempotency ledger, with no schema redesign;
- same-key different-payload requests return typed conflicts;
- no raw prompt, raw model output, or privacy-sensitive household/profile payload is persisted or logged;
- no Telegram wiring changes are introduced in C5A.

Tests:

- optimistic concurrency
- idempotent retry
- duplicate callback delivery
- role-based publish and archive authorization
- LLM malformed output rejection
- LLM retry no-duplicate guarantee
- publish snapshot immutability
- owner-only runtime refusal for adult-admin actors
- no-transaction-held-during-provider-call proof
- stale-state conflict without partial generated writes

Feature state:

- disabled by default
- controlled canary only

Production impact:

- mutation canary on explicit approved users only
- no automatic diary writes

Rollback:

- disable feature
- image rollback
- preserve created menu history

Stop conditions:

- duplicate menu lineage
- last-write-wins behavior
- partial writes
- raw prompt or output privacy leak

### C6 - Shopping UI and Mutations

Goal:

- expose shopping list views and controlled mutations, including manual items and regeneration.

Expected files:

- shopping UI renderers
- callback handlers
- manual item, toggle, and regenerate services

Schema changes:

- none expected

Runtime changes:

- `🛒 Список покупок` becomes functional behind shopping allowlist
- deterministic regenerate behavior
- manual item preservation contract enforced

Tests:

- standalone list support
- menu-linked derivation
- exact immutable source-menu linkage
- checked-state preservation
- manual-item preservation
- generated-item override semantics
- unit incompatibility behavior
- duplicate callback and idempotency behavior

Feature state:

- disabled by default
- controlled shopping allowlist only

Production impact:

- limited mutation canary
- no effect on diary, profile, weight, water, or reminder tables

Rollback:

- disable shopping feature
- image rollback
- preserve append-only history

Stop conditions:

- manual items disappear
- checked state silently resets
- incompatible units merge incorrectly

### C7 - Family UI

Goal:

- expose household member-oriented family management on top of the existing household foundation.

Expected files:

- family UI service layer
- authorization-aware handlers
- member read and update flows within approved scope

Schema changes:

- only if required by approved family-specific design

Runtime changes:

- `👨‍👩‍👧‍👦 Семья` becomes functional behind its own feature gate
- family UI must reuse canonical household authorization context

Tests:

- member authorization
- inactive member refusal
- owner and admin scope differences
- cross-household refusal

Feature state:

- disabled by default
- separate allowlist from menu and shopping

Production impact:

- narrow canary only

Rollback:

- disable family UI feature
- image rollback

Stop conditions:

- cross-household access
- member scope confusion
- accidental coupling to Telegram identity

## Migration and Runbook Planning

Every future stage that touches schema or production rollout must follow this sequence:

1. schema rehearsal on non-production copy
2. production backup
3. exact-image deployment with feature disabled
4. additive schema initialization
5. canonical audit
6. feature-disabled proof
7. allowlist canary
8. rollback-ready validation

No stage in this plan authorizes an executable production migration script today.

## Production Readiness Stages After C5A

The post-C5A rollout path is intentionally split into separate approved stages:

### D0 - Feature-Disabled Production Readiness Audit and Rollout Plan

Scope:

- read-only source and config audit;
- exact-image contract documentation;
- feature-disabled rollout runbook;
- rollback taxonomy;
- production-readiness contract test.

Explicit exclusions:

- no build;
- no deploy;
- no production DB open;
- no schema initialization;
- no feature enablement;
- no allowlist population.

### D1 - Exact Image Build and Offline Validation

Scope:

- build exact approved SHA only;
- verify embedded full Git SHA and image ID;
- run focused tests and agent check before build;
- prove startup/import paths stay side-effect free.

Explicit exclusions:

- no production deploy;
- no Qdrant change;
- no schema initialization;
- no feature enablement.

### D2 - Feature-Disabled Hermes-Only Deployment

Scope:

- deploy exact validated image;
- recreate Hermes only;
- keep weekly and shopping features disabled;
- keep allowlists empty;
- prove placeholders and existing product surface remain healthy.

Explicit exclusions:

- no Qdrant recreate;
- no schema initialization;
- no weekly/shopping business-row creation;
- no feature canary.

### D3 - Explicit Weekly/Shopping Production Schema Initialization

Scope:

- production backup before first DDL;
- explicit weekly schema initialization first;
- explicit shopping schema initialization second;
- zero business-row verification;
- existing data preservation proof.

Explicit exclusions:

- no feature enablement;
- no Telegram mutation UI rollout;
- no allowlist canary.

### D4 - Disabled-State Observation and Rollback Verification

Scope:

- observe feature-disabled runtime after schema init;
- verify no provider calls, no unexpected writes, and stable placeholders;
- verify image-only rollback and post-schema rollback logic.

Explicit exclusions:

- no canary enablement;
- no shopping or weekly mutations from Telegram.

### D5 - Later Allowlist Canary

Scope:

- weekly/shopping allowlist canary only after D1-D4 complete and are separately approved.

Explicit exclusions:

- not part of D0, D1, D2, D3, or D4;
- not bundled with Telegram mutation UI rollout by default.

## Feature Flag Matrix

```text
HEALBITE_WEEKLY_MENU_ENABLED=false
HEALBITE_WEEKLY_MENU_ALLOWLIST=

HEALBITE_SHOPPING_LIST_ENABLED=false
HEALBITE_SHOPPING_LIST_ALLOWLIST=

HEALBITE_FAMILY_UI_ENABLED=false
HEALBITE_FAMILY_UI_ALLOWLIST=
```

Household foundation eligibility is necessary but not sufficient. Future access requires:

```text
resolved household authorization context
+
feature-specific enablement
+
feature-specific allowlist approval
```

## Rollout Safety Contract

For each future implementation PR, record:

- goal
- expected files
- schema changes
- runtime changes
- tests
- feature state
- production impact
- rollback
- stop conditions

No implementation stage may skip those headings in its rollout or review document.

## Threat Review by Stage

Across C1-C7, the following must be rechecked:

- cross-household access
- forged callback IDs
- stale callbacks
- duplicate Telegram delivery
- LLM malformed output
- LLM duplicate retry
- partial DB transaction
- incorrect shopping dedup
- PII leakage
- raw IDs in logs
- feature misconfiguration
- startup side effects

## Explicit Non-Goals for This Plan

This plan does not authorize implementation of:

- recipe catalog or pantry
- inventory or store integrations
- delivery ordering or pricing
- shared medical profile
- automatic diary recording from plans
- automatic purchase workflows
- real-time collaboration
