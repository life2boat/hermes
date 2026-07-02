# ADR-0072: Household Foundation Bootstrap

Status: Proposed

Date: 2026-07-03

## Context

Sprint 7.1A established the household-first domain model:

```text
single user = household with one primary member
```

Sprint 7.1B0 plans the implementation contract for household foundation only.
It does not implement runtime code, migrations, schema changes, Telegram UI,
weekly menu, shopping list, family editing, LLM generation, build, deploy, or
production restart.

Current HealBite identity is legacy application identity backed by the HealBite
SQLite profile/user tables. Code supports `users.user_id` or
`users.telegram_id` through compatibility helpers. Production currently exposes
`users.telegram_id`; related history tables use `user_id`.

## Decision

Use `users` as the authoritative existing-user source for household bootstrap.

The `users` table is the application identity registry for HealBite onboarding,
profile target storage, and profile lookup. `profiles`, `nutrition_log`,
`weight_entries`, `water_intake_events`, Memory OS tables, and reminder tables
are compatibility/history inputs, not authoritative bootstrap sources.

Admin allowlists are explicitly not a user registry and must not be used as a
bootstrap source.

## Identity Bridge

The initial bridge is:

```text
legacy application actor ID
-> household_members.linked_user_id
```

Today the legacy application actor ID may be numerically equal to a Telegram
user ID. This is a compatibility fact, not a household-domain primary key.

Household and member primary keys must be internal stable IDs. Future identity
abstraction can replace the legacy actor source without changing
`household_members.id`.

## Scope of Sprint 7.1B Implementation

Minimal 7.1B foundation:

- additive `households` table;
- additive `household_members` table;
- schema initializer;
- store methods for lookup and atomic personal household creation;
- authorization context resolver;
- feature defaults disabled;
- no weekly menu generation;
- no shopping list;
- no family editing UI;
- no migration of existing diary, weight, water, or profile history.

Nutrition target remains bridged from the existing profile/user target sources
for 7.1B. A dedicated member nutrition target table is deferred until weekly
menu planning needs immutable target snapshots.

## Proposed Schema

DDL is illustrative Markdown only. It is not a migration script.

```sql
CREATE TABLE households (
  id TEXT PRIMARY KEY,
  owner_user_id INTEGER NOT NULL,
  name TEXT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'closed')),
  default_timezone TEXT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE household_members (
  id TEXT PRIMARY KEY,
  household_id TEXT NOT NULL,
  linked_user_id INTEGER NULL,
  display_name TEXT NULL,
  member_type TEXT NOT NULL CHECK (
    member_type IN ('primary', 'linked_adult', 'unlinked_adult', 'dependent')
  ),
  role TEXT NOT NULL CHECK (role IN ('owner', 'adult_admin', 'adult_member', 'dependent')),
  status TEXT NOT NULL CHECK (status IN ('active', 'unlinked', 'disabled', 'removed')),
  age_band TEXT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_household_members_active_linked_user
ON household_members(linked_user_id)
WHERE linked_user_id IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX idx_household_members_active_owner
ON household_members(household_id)
WHERE role = 'owner' AND status = 'active';
```

## Primary Key Strategy

Use internal opaque TEXT IDs for `households.id` and `household_members.id`.

Recommended implementation: random UUIDv4 or equivalent opaque generated ID.
Do not derive the ID from Telegram ID or legacy user ID. Idempotency is provided
by unique constraints and transactional get-or-create, not by reversible IDs.

Rationale:

- SQLite-compatible;
- simple foreign keys;
- safe backup/rebuild behavior;
- no raw Telegram ID exposure through domain IDs;
- future API compatibility;
- deterministic tests can inject generated IDs.

## Ownership Contract

`households.owner_user_id` is the legacy application actor ID for the current
owner. It is `NOT NULL` for personal households in 7.1B.

Owner removal requires ownership transfer or explicit household closure. A
disabled/deleted linked identity must not cascade-delete household history.

## Status Contracts

Household:

```text
active -> disabled -> active
active -> closed
disabled -> closed
```

Member:

```text
active -> unlinked
active -> disabled
unlinked -> active
unlinked -> disabled
disabled -> active
disabled -> removed
```

`invited` is deferred until linked-adult invitation flows exist.

## Delete and Cascade Contract

Use `ON DELETE RESTRICT` for household-owned rows. Household closure and member
removal are soft state transitions. Do not use cascade deletion for member
history, future weekly plans, or shopping history.

Unlinking a Telegram/application identity sets member status or link state; it
does not delete member history.

## Bootstrap Strategy

Use a hybrid strategy:

1. eager bootstrap for eligible rows in `users`;
2. lazy `get_or_create_personal_household(actor_user_id)` for new eligible
   users after deployment.

Eager bootstrap covers known application users. Lazy bootstrap protects future
users and rare actors without requiring historical backfill from every history
table.

## Bootstrap Invariants

For each eligible application user:

```text
exactly one active personal household
exactly one active primary linked member
member.household_id points to that household
member.linked_user_id equals the legacy actor ID
member.role=owner
member.status=active
household.status=active
```

Repeated bootstrap must create zero semantic delta.

## Inconsistent State Handling

Migration and bootstrap must detect and stop on:

- two households for one linked user;
- two active owners in one household;
- household without active owner;
- member with missing household;
- linked user already attached to another active household;
- partial bootstrap row.

Do not destructively auto-merge households. Report safe aggregate counts and
require manual remediation.

## Feature Flags

Proposed flags:

```text
HEALBITE_HOUSEHOLDS_ENABLED=false
HEALBITE_HOUSEHOLDS_ALLOWLIST=
```

Semantics:

- `enabled=false`: current profile, diary, weight, water, weekly report, and
  Memory OS flows work without household dependency.
- `enabled=true` with allowlist: household-aware runtime is available only for
  selected actors.
- empty allowlist is not global rollout.
- additive schema creation can run independently of UI feature enablement, but
  runtime household routes remain disabled.

## Rollback Strategy

Because the schema is additive:

- runtime rollback means `HEALBITE_HOUSEHOLDS_ENABLED=false`;
- new tables may remain in SQLite;
- existing user-scoped features do not depend on household tables;
- do not automatically `DROP TABLE` in production;
- do not restore DB unless there is proven DB corruption or data loss and a
  separate operator decision.

## Privacy

Reports and logs may include only safe aggregates:

```text
eligible_user_count
created_household_count
created_member_count
already_bootstrapped_count
invalid_actor_count
duplicate_conflict_count
orphan_count
duration_bucket
error_type
```

Do not log user IDs, Telegram IDs, names, profile content, nutrition values,
chat IDs, raw rows, or raw exception bodies.
