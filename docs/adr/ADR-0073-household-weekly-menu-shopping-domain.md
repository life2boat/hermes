# ADR-0073: Household Weekly Menu and Shopping Domain

Status: Proposed

Date: 2026-07-04

## Context

Sprint 7.1B finished the household foundation rollout with these stable facts:

- production household schema is canonical;
- four personal households already exist;
- household feature remains disabled;
- household allowlist remains empty;
- Telegram placeholders for weekly menu, shopping list, and family remain unchanged;
- existing member-scoped flows for profile, diary, weight, water, and reports remain healthy.

The next product surface needs a design lock before any schema or runtime implementation:

- household weekly menu;
- household shopping list;
- future family UI built on the same household aggregate.

This ADR must freeze domain boundaries, ownership, week semantics, lifecycle, authorization, migration sequence, and rollout safety without adding runtime code, database schema, or production writes.

## Decision

Use two separate household-scoped aggregates:

1. `weekly menu`, keyed logically by `household_id + week_start`, with revisioned snapshots.
2. `shopping list`, keyed by an opaque list ID and optionally linked to a specific menu revision.

Member-scoped health data remains canonical in existing tables. Weekly menu and shopping list never become sources of truth for diary, profile, weight, water, or nutrition-target history.

## Aggregate Boundaries

### Weekly Menu

The weekly menu aggregate belongs to a household and covers exactly one canonical week.

Logical key:

```text
household_id + week_start
```

Physical revision identifier:

```text
menu_id = opaque lowercase canonical UUIDv4
```

There is one canonical menu series per household/week. That logical series is identified by:

```text
household_id + week_start
```

Each physical `household_weekly_menus` row is one immutable menu revision snapshot inside that series. `menu_id` identifies that exact physical revision row and is the opaque identifier future callbacks and shopping-link rows must carry when they need an exact immutable snapshot reference.

### Shopping List

The shopping list aggregate also belongs to a household.

Physical identifier:

```text
shopping_list_id = opaque lowercase canonical UUIDv4
```

A shopping list may be:

- derived from a published menu revision; or
- standalone and manual.

Shopping is a separate aggregate, not a child collection embedded inside the menu aggregate. This keeps shopping regeneration, checked-state handling, and manual-item preservation independent from menu publishing.

## Identity and Ownership

### Ownership

- weekly menu belongs to `household`;
- shopping list belongs to `household`;
- actor access is resolved through an active linked `household member`;
- linked_user_id is not a household ID;
- member_id is not interchangeable with user_id.

### Required Future Call Contract

All future store/service mutations must require:

```text
household_id
actor member context
expected authorization scope
```

The future API surface must not infer authorization from raw Telegram callback payloads. Callback payloads may carry opaque aggregate IDs, item IDs, expected versions, and idempotency tokens, but server-side authorization must always re-resolve membership and household scope before any read or write.

## Authorization

The future design must use `HouseholdAuthorizationContext` from the canonical household foundation. Weekly menu and shopping operations must fail closed when:

- actor is missing;
- actor is not linked to an active household member;
- member status is inactive or removed;
- household status is not active;
- requested household does not match the authorized household in context.

The household domain must never trust raw `household_id`, `member_id`, `user_id`, or Telegram transport identifiers as proof of access.

### Role Permissions

Until a narrower future ADR changes the policy explicitly, the default mutation contract is:

```text
owner:
  may create/replace draft menus
  may publish/archive menus
  may create/regenerate/archive shopping lists

adult_admin:
  may create/replace draft menus
  may publish/archive menus
  may create/regenerate/archive shopping lists

adult_member:
  may view menu and shopping
  may propose or edit draft content only if the specific future surface explicitly enables it
  may not publish/archive a menu revision
  may not archive a shopping aggregate by default

dependent:
  read-only or no access, depending on future UI surface
  no mutation rights
```

If an implementation stage cannot enforce those differentiated permissions safely, that stage must remain disabled instead of widening mutation access to every active member.

## Single-Member Compatibility

Single-member households remain first-class citizens. A current solo HealBite user with one active personal household member gets the same functional UX as a classic single-user flow. The system does not introduce a separate legacy single-user menu domain.

## Week Semantics

Canonical week contract:

```text
week_start is a local calendar date
week_start always represents Monday
one week = 7 consecutive local dates
storage is ISO date YYYY-MM-DD
storage is not derived from job execution time or Docker host locale
```

### Timezone Source Order

1. household timezone, when present;
2. explicitly configured application timezone;
3. documented safe default.

Timezone must not be inferred from Telegram server location or host locale.

### Normalization Decision

External callers may provide any date within the intended week. A single normalization helper must convert it to the canonical Monday `week_start` before persistence. Store-level uniqueness and validation operate on the normalized Monday date only. Direct writes that bypass the helper and submit a non-Monday `week_start` must be rejected by validation.

## Weekly-Menu Lifecycle

Minimal lifecycle states:

```text
draft
published
archived
```

### Active Aggregate Contract

- one canonical menu series exists per `household_id + week_start`;
- each physical revision snapshot has its own `menu_id`;
- `revision_number` is an integer and increases monotonically within one logical series;
- the latest published revision is a stable snapshot;
- there may be at most one active draft revision per logical series;
- there may be at most one active published revision per logical series;
- published revisions are immutable snapshots.

### Revision Identity Contract

The future implementation must keep the logical weekly-menu series and the physical immutable revision row distinct:

```text
logical menu series key = household_id + week_start
physical revision row ID = menu_id
human/comparison ordering = revision_number
```

`menu_id` is the identifier for exactly one immutable revision snapshot, not a mutable pointer to "whatever is current now". `revision_number` is not a substitute for `menu_id`; it is only the monotonic ordinal inside the same logical series.

The future callback contract may carry:

```text
menu_id
expected_version
idempotency_token
```

and may include `revision_number` for display/debug safety, but server-side authorization and lookup must still resolve the exact revision row by trusted household scope.

### Editing Published Menus

Editing an already published menu must not mutate the published snapshot in place. The explicit contract is:

- publish freezes the revision;
- later changes create a new draft revision derived from the last published content or a new generated proposal;
- a new explicit publish makes that later draft the new published revision;
- the previous published revision is retired from the active published slot in the same transaction and becomes a historical archived snapshot;
- archive retires the lineage from active use but keeps history.

This avoids ambiguous in-place edits and gives stable shopping linkage and auditability.

## Meal-Entry Model

Menu entries are planned meals, not consumption facts.

### Entry Identity

Each entry uses an opaque canonical lowercase UUIDv4.

### Required Fields

Each future menu entry must define:

```text
entry_id
menu_id
revision_number
household_id
local_date
meal_slot
position
servings
origin
created_at
updated_at
version
```

Optional but expected fields:

```text
title
description
recipe_ref
source_ref
structured_nutrition_snapshot
generation_metadata_snapshot
```

### Day Representation

Use local calendar `date`, not only day index, as the canonical persisted value. Day index may be derived in UI, but the domain stores a real local date inside the week range.

### Meal Slots

Meal slots must be normalized. Minimal validated value set:

```text
breakfast
lunch
dinner
snack
```

The value set may be extended later, but not replaced with unbounded free text.

### Origin

At minimum:

```text
generated
manual
copied
```

The entry origin must survive edits for debugging, regeneration, and product analytics.

## Food Diary Separation

The weekly menu is a planning surface only.

Explicit contract:

- menu entry creation does not write to `nutrition_log`;
- publishing a menu does not write to `nutrition_log`;
- opening, viewing, archiving, or regenerating a menu does not write to `nutrition_log`;
- any future copy-from-plan-to-diary operation must be explicit and separately authorized.

## Member Nutrition Bridge

Weekly menu is household-scoped, but member nutrition context stays member-scoped.

### Bridge Decision

Future menu generation may consume read-only member preference and target inputs, but the menu aggregate stores only the minimum necessary generation snapshot or metadata for explanation and reproducibility.

Canonical sources remain existing tables and stores for:

- profile;
- nutrition targets;
- food diary;
- weight;
- water;
- reports.

Published menu revisions must not be automatically rewritten when a profile or nutrition target changes later.

The household domain must not create a shared household medical profile. Member-specific constraints remain scoped to the member.

## Shopping-List Lifecycle

Minimal shopping lifecycle:

```text
draft
active
completed
archived
```

### Source Contract

A shopping list may carry:

```text
source_menu_id
source_menu_revision
```

or be standalone with no source menu linkage.

`source_menu_id` references the exact immutable published menu revision snapshot row. `source_menu_revision` is optional denormalized metadata for diagnostics and human comparison; it must never replace the exact immutable `source_menu_id` reference.

### Lifecycle Decisions

- standalone manual shopping list is allowed;
- shopping derivation from menu is an explicit operation;
- a new menu revision does not silently mutate an existing active or completed shopping list;
- regeneration for the same draft shopping list operates only on generated items unless the user explicitly replaces the entire list;
- completed shopping lists are immutable except for explicit archival.

### Active List Contract

For a given household/week, at most one shopping list may be `active` at a time. Additional lists for the same week must remain `draft`, `completed`, or `archived`.

## Menu-to-Shopping Derivation

Shopping derivation should be deterministic when possible and should not depend on opaque LLM side effects.

Generation rules:

- derive only from the referenced menu revision;
- preserve manual items on regeneration unless explicit replace-all is chosen;
- preserve checked state through deterministic item matching when regenerating the same list lineage;
- if generated items disappear because the source menu changed, the system must classify them explicitly as removed or replaced rather than silently clearing user state.

## Manual Shopping Items

Manual items are first-class rows inside the shopping aggregate.

Required origin values:

```text
menu_generated
manual
```

Manual items must not disappear on shopping regeneration. If a generated item is manually edited, the system must promote it to an override contract rather than continue treating it as a replaceable generated row.

## Shopping Item Contract

Each future shopping item must contain:

```text
item_id
shopping_list_id
household_id
normalized_name
display_name
quantity_value
quantity_unit_normalized
quantity_unit_display
category
position
checked_state
origin
source_menu_entry_id (optional)
version
created_at
updated_at
```

Food diary items must not be reused as shopping items.

## Quantity and Unit Semantics

Persist quantity as a deterministic decimal-safe representation, not binary float.

Decision:

- use decimal string for persisted quantity values;
- keep normalized unit vocabulary for machine semantics;
- keep display unit for user-facing text;
- allow `unitless` and `unknown` explicitly;
- never auto-merge incompatible units.

Canonical quantity contract for the first implementation:

```text
syntax: ^[0-9]+(\.[0-9]{1,3})?$
nonnegative only
no exponent notation
no sign prefix
no NaN/Infinity
locale-independent decimal point only
maximum precision = 12 total digits
maximum scale = 3 fractional digits
```

Normalization must remove insignificant trailing fractional zeroes and must reject locale-specific comma separators in persisted values.

Examples of non-mergeable semantics:

```text
grams vs pieces
milliliters vs packages
free-text units with unknown conversion
```

If unit compatibility is unknown, keep items separate.

## Deduplication Contract

Deduplication must be deterministic and conservative.

The future generated-item dedup key should consider at least:

```text
household_id
shopping_list_id or source lineage
normalized ingredient identity
normalized unit
category
origin
manual override state
```

Deduplication must not be based only on lowercase display text.

## Checked-State Preservation

Checked state must not be silently reset during regeneration.

Contract:

- preserve checked state for matching item lineage when dedup key is stable;
- preserve all manual items;
- mark removed generated items as removed or archived from the regenerated draft rather than silently clearing every check;
- completed lists do not get silently reopened by menu regeneration.

## Versioning and Concurrency

Every mutable aggregate and mutable entry/item row must include:

```text
integer version
updated_at
```

Every future mutation method must require:

```text
expected_version
```

On stale version:

```text
conflict
no partial write
no hidden last-write-wins fallback
```

## Idempotency

The future service boundary must accept an idempotency token for mutation-like operations, including:

```text
create weekly menu
generate menu
publish menu
derive shopping list
regenerate shopping list
toggle item
archive aggregate
```

Duplicate Telegram callback deliveries, timeout retries, job retries, or LLM retries must resolve to the same logical result and must not create duplicate menus, lists, or items.

Idempotency scope must include at least:

```text
household_id
authorized actor member
operation type
target aggregate identity
opaque request token
```

If the same token is replayed with the same payload, the service returns the original logical result. If the same token is replayed with a different payload, the service must reject it as a conflict instead of silently reusing or overwriting prior state.

## Transaction Boundaries

The following operations must be atomic:

- create aggregate;
- replace draft entries;
- publish revision;
- derive shopping list;
- regenerate generated items;
- archive aggregate.

Failure contract:

- no partial menu;
- no orphan entries;
- no list without promised items;
- no version increment on rollback;
- no partial toggle or partial regenerate state.

## Deletion and Archival

Default policy: archive, do not hard delete.

### Allowed Behavior

- archived menu and list history remains queryable for audit and UX history;
- hard deletion is reserved for empty, unreferenced draft or test records through controlled maintenance only;
- deleting a household must not cascade into existing member-scoped health tables;
- shopping source linkage must preserve the menu revision snapshot it was derived from.

No first-stage production design should assume automatic deletion of household planning history.

## UUID Contract

All new aggregate and row IDs in this domain must be opaque lowercase canonical UUIDv4 values.

Do not use:

- Telegram IDs;
- user IDs;
- rowid;
- `household_id + date` as public IDs;
- sequential counters.

## Foreign-Key Policy

Future tables must enforce household isolation and orphan prevention, while avoiding destructive cascade into existing health tables.

Contract:

- household-scoped menu and list rows reference household tables with `ON DELETE RESTRICT` or equivalent fail-closed behavior;
- entry and item rows reference parent aggregates with restrictive or archive-aware behavior;
- menu and shopping domain never owns profile, diary, weight, water, or reminder tables;
- member deactivation must not destroy menu or shopping history.

## Database Schema Proposal

The first implementation stage should propose additive tables only. Preferred names:

```text
household_weekly_menus
household_weekly_menu_entries
household_shopping_lists
household_shopping_items
```

Recommended supporting fields and constraints:

### `household_weekly_menus`

- primary key: `id TEXT PRIMARY KEY`;
- foreign key: `household_id -> households(id)`;
- one row = one immutable revision snapshot;
- logical series key: `(household_id, week_start)`;
- revision ordering key: `(household_id, week_start, revision_number)` unique;
- partial uniqueness for at most one draft and at most one published row per logical series;
- status check;
- version check `>= 1`;
- week_start check enforced by helper plus weekday validation;
- timestamps;
- optional published metadata;
- no business-row auto creation on startup.

### `household_weekly_menu_entries`

- primary key: `id TEXT PRIMARY KEY`;
- foreign key: `menu_id -> household_weekly_menus(id)`;
- `household_id` repeated for isolation and query safety;
- `local_date` inside week range;
- normalized `meal_slot`;
- deterministic `position` ordering;
- version and timestamps;
- restrictive parent delete and archive policy.

### `household_shopping_lists`

- primary key: `id TEXT PRIMARY KEY`;
- foreign key: `household_id -> households(id)`;
- optional `source_menu_id`, `source_menu_revision`;
- `source_menu_id` references the exact immutable published revision row, not only the logical week series;
- status check;
- version and timestamps;
- uniqueness rules for active-list semantics.

### `household_shopping_items`

- primary key: `id TEXT PRIMARY KEY`;
- foreign key: `shopping_list_id -> household_shopping_lists(id)`;
- `household_id` repeated for isolation;
- origin check;
- checked-state field;
- decimal-safe quantity string;
- normalized and display unit split;
- deterministic ordering;
- version and timestamps.

## Household Isolation

Every future query and mutation must scope by household and authorization context:

```text
SELECT ... WHERE id=? AND household_id=?
UPDATE ... WHERE id=? AND household_id=? AND version=?
```

Cross-household access must fail closed.

## Feature Flags

Do not reuse the household foundation flag for all future features.

Separate flags:

```text
HEALBITE_WEEKLY_MENU_ENABLED
HEALBITE_WEEKLY_MENU_ALLOWLIST

HEALBITE_SHOPPING_LIST_ENABLED
HEALBITE_SHOPPING_LIST_ALLOWLIST

HEALBITE_FAMILY_UI_ENABLED
HEALBITE_FAMILY_UI_ALLOWLIST
```

Default state:

```text
disabled
empty
fail closed
```

Feature access should require:

```text
household foundation resolver
+
feature-specific gate
```

## Telegram Boundary

Until a later UI sprint, Telegram remains unchanged:

```text
📋 Меню на неделю -> В разработке
🛒 Список покупок -> В разработке
👨‍👩‍👧‍👦 Семья -> В разработке
```

No new production callback contract is reserved in this stage.

### Future Callback Security Contract

Future callback payloads must use:

```text
opaque aggregate and item IDs
server-side actor authorization
server-side household authorization check
expected version
idempotency token
no raw household or user IDs
```

## Privacy

Do not log raw household IDs, member IDs, Telegram IDs, nutrition values, or shopping payload details when planning operations or generation retries fail.

Menu generation snapshots must retain only the minimum necessary structured data. The system must not create a shared household medical profile.

## LLM Boundary

The future menu generator may use an LLM, but the LLM is never source of truth.

Contract:

- structured output must be validated before any transaction;
- domain writes occur only after validation;
- LLM retry must be idempotent;
- generation failure must not damage or partially mutate an existing menu;
- raw prompt and output must avoid unnecessary PII persistence;
- shopping derivation should remain deterministic where possible.

## Migration Strategy

Future implementation sequence:

```text
C1 - weekly-menu schema/store core
C2 - shopping schema/store core
C3 - feature-disabled runtime services
C4 - Telegram read-only menu UI behind allowlist
C5 - controlled menu mutations and generation
C6 - shopping UI and mutations
C7 - family UI
```

Each stage must be:

- a separate PR;
- default disabled;
- exact-image deployable;
- auditable;
- rollback-safe;
- free of implicit startup writes.

## Production Rollout

Recommended future rollout sequence:

1. merge schema and store code;
2. exact-image deploy with feature disabled;
3. run additive schema initialization only;
4. run canonical audit;
5. verify feature-specific allowlists are empty;
6. run internal non-production tests;
7. run controlled production allowlist canary;
8. run read-only UI canary;
9. run mutation canary;
10. expand rollout gradually.

Existing four production households must not be auto-enabled as a cohort. Every feature enablement requires explicit operator approval.

## Alternatives Considered

### Keep a Single-User Legacy Weekly Menu Domain

Rejected. It would duplicate ownership semantics and create a later migration cliff when household planning expands.

### Store Shopping as Embedded Menu Rows

Rejected. Shopping lifecycle, regeneration, checked-state preservation, and standalone manual lists need an independent aggregate.

### Use Raw Telegram IDs as Aggregate IDs

Rejected. They leak transport identity, weaken privacy, and break future transport-agnostic domain boundaries.

### Mutate Published Menu In Place

Rejected. It weakens auditability, shopping linkage, and concurrency semantics.

## Consequences

Positive:

- household planning and member-scoped health data stay separated;
- current solo-user UX remains compatible;
- feature-disabled rollout remains possible;
- optimistic concurrency and idempotency are built in early;
- menu and shopping can evolve independently.

Trade-offs:

- more aggregate and revision bookkeeping;
- explicit shopping derivation and regeneration logic;
- stricter callback and authorization contracts;
- more migration stages before full UI rollout.

## Threat Model

This design explicitly guards against:

- cross-household access;
- forged callback IDs;
- stale callbacks;
- duplicate Telegram delivery;
- LLM malformed output;
- LLM duplicate retry;
- partial DB transaction;
- incorrect shopping dedup;
- PII leakage;
- raw IDs in logs;
- feature misconfiguration;
- startup side effects.

## Explicit Non-Goals

This stage does not design or authorize:

- recipe catalog implementation;
- pantry or inventory tracking;
- grocery-store integrations;
- price comparison;
- delivery ordering;
- medical diagnosis;
- shared household medical profile;
- automatic food-diary recording;
- automatic shopping purchase;
- real-time collaborative editing.
