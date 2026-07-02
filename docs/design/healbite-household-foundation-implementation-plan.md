# HealBite Household Foundation Implementation Plan

Status: design-only plan

Base architecture: ADR-0071 and ADR-0072.

## Current Identity and Schema Findings

Current HealBite identity is user-scoped and uses a legacy integer actor ID.
The profile store supports either `users.user_id` or `users.telegram_id`.
Production currently has `users.telegram_id`; related event/history tables use
`user_id`.

Reviewed modules:

- `gateway/healbite_user_profile.py`
- `gateway/healbite_nutrition_diary.py`
- `gateway/healbite_weight_tracker.py`
- `gateway/healbite_water_tracker.py`
- `gateway/healbite_weight_reminder_schema.py`
- `gateway/platforms/telegram.py`
- `gateway/authz_mixin.py`
- `gateway/memory/qdrant_adapter.py`

Safe aggregate production facts observed during read-only inspection:

```text
db_integrity=ok
users_distinct_positive=6
profiles_distinct_positive=4
nutrition_log_distinct_positive=3
weight_entries_distinct_positive=1
water_intake_events_distinct_positive=1
union_distinct_positive=6
```

No production rows, user IDs, names, profile values, weight values, nutrition
values, chat IDs, or Telegram IDs were inspected or documented.

## Authoritative Bootstrap Source

Authoritative source: `users`.

Rationale:

- it is created and maintained by onboarding/profile code;
- it stores the application identity used by profile targets;
- production union across profile/history tables does not add actors beyond
  `users`;
- history tables are not identity registries;
- admin allowlist is not a registry and must not be used.

Eligibility:

- positive integer application actor ID in `users`;
- not a bot/system actor when such metadata is available;
- not deleted/disabled if future `access_status` semantics indicate deletion.

Users with profile/history but no `users` row are handled by rehearsal conflict
reporting and future lazy bootstrap, not silent broad union bootstrapping.

## Identity Bridge

```text
Telegram/private-chat identity -> legacy application actor ID -> linked_user_id
```

`linked_user_id` references the application actor identity. In the current
Telegram-only HealBite path this may equal a Telegram numeric ID, but household
member identity is internal and stable.

## Minimal Store API Contract

Sprint 7.1B implementation subset:

```text
create_household_schema
get_household_by_id
get_household_for_linked_user
get_primary_member_for_user
get_or_create_personal_household
list_household_members
resolve_actor_household_context
assert_household_access
```

Deferred APIs:

```text
create_dependent_member
update_member
disable_member
transfer_ownership
```

## Service and Store Boundaries

Store owns:

- SQLite CRUD;
- transactions;
- constraints;
- compare-and-swap;
- row hydration.

Store must not call Telegram, LLM, Memory OS, or UI builders.

Service owns:

- actor authorization;
- bootstrap decisions;
- role checks;
- status transitions;
- business invariants.

Telegram layer owns:

- trusted actor resolution;
- calling the service;
- rejecting arbitrary `household_id` or `member_id` from callback/text without
  authorization context.

## Authorization Context

Typed context:

```text
actor_user_id
household_id
household_member_id
role
member_status
household_status
```

All household-aware operations must receive context from a trusted resolver.
Never trust household/member IDs from callback data or user text as
authorization proof.

## Cross-Household Protection

Required query patterns:

```text
SELECT ... WHERE id=? AND household_id=?
UPDATE ... WHERE id=? AND household_id=? AND version=?
```

Where SQLite cannot enforce composite household invariants directly, combine:

- composite unique keys;
- composite foreign keys where practical;
- service-level assertions;
- integration tests.

## Atomic Get-Or-Create

Future operation:

```text
get_or_create_personal_household(actor_user_id)
```

Requirements:

- one transaction boundary;
- reject nonpositive, bot, or system actors;
- insert household and owner member atomically;
- backed by unique active linked-user constraint;
- retry lookup after `IntegrityError` from concurrent creation;
- repeated call returns the same household/member;
- no correlation ID dependency.

## Compatibility Layer

Existing features remain user-scoped:

- profile;
- food diary;
- weight;
- water;
- weekly report;
- Memory OS.

Sprint 7.1B does not rewrite existing historical tables to household/member IDs.
The bridge is:

```text
existing user ID -> primary household member
```

Future weekly menu and shopping modules use member/household IDs without
breaking current tables.

## Nutrition Target Bridge

7.1B does not create a new nutrition target table. The primary member resolves
nutrition targets from existing profile/user target sources through a bridge
API.

Future 7.1C can add immutable target snapshots for weekly plan generation.
Avoid duplicating current nutrition data before the weekly plan needs it.

## Idempotency and Concurrency

Contracts:

- unique active linked-user partial index prevents duplicate active personal
  memberships;
- unique active owner partial index prevents two active owners;
- bootstrap is order-independent;
- second bootstrap creates zero semantic delta;
- concurrent get-or-create returns one household;
- service retries read after unique conflict;
- version/CAS protects future edits.

## Failure Model

Stop safely on:

- schema creation failure;
- foreign key support disabled;
- duplicate mapping;
- concurrent lazy bootstrap conflict not resolved by retry;
- DB locked beyond retry budget;
- disk full;
- corrupt DB;
- partial migration batch;
- missing authorization context;
- unknown member/household status.

Requirements:

- feature remains disabled;
- existing user features continue to work;
- no destructive rollback;
- no automatic table deletion;
- no continuation after integrity failure.

## Observability Contract

Marker families:

```text
[HealBite][household_schema]
[HealBite][household_bootstrap]
[HealBite][household_access]
```

Safe fields:

```text
action
outcome
batch_size_bucket
created_count_bucket
existing_count_bucket
conflict_count_bucket
role
member_type
household_status
error_type
duration_bucket
```

Forbidden fields:

```text
household ID
member ID
linked user ID
Telegram ID
display name
profile values
raw exception body
```

## Test Strategy

Schema:

- first initializer;
- second initializer;
- enum checks;
- indexes;
- foreign keys;
- partial unique indexes.

Store:

- create personal household;
- get existing household;
- get primary member;
- list members;
- version/CAS;
- invalid status.

Idempotency:

- repeated bootstrap;
- concurrent get-or-create;
- retry after unique conflict.

Authorization:

- cross-household access rejected;
- disabled household rejected;
- disabled member rejected;
- dependent restrictions;
- forged household/member ID rejected.

Migration:

- no existing users;
- one user;
- many users;
- partial previous bootstrap;
- duplicate conflict;
- rerun;
- non-household counts preserved.

Privacy:

- IDs absent from logs;
- names absent;
- raw exception absent;
- safe aggregate markers present.

Compatibility:

- profile unchanged;
- food diary unchanged;
- weight unchanged;
- water unchanged;
- weekly report unchanged;
- Memory OS isolation unchanged.

Property/concurrency:

- one linked user cannot have two active personal households;
- one household cannot have two active owners;
- parallel get-or-create returns one household;
- bootstrap order does not change final state;
- second bootstrap produces zero semantic delta.

## Implementation PR Breakdown

### PR 7.1B1: Schema and Store Core

Scope:

- schema constants;
- additive initializer;
- household/member stores;
- atomic get-or-create;
- tests;
- feature defaults false.

Exclusions:

- production backfill;
- Telegram UI;
- family editing;
- weekly menu.

### PR 7.1B2: Bootstrap and Migration Tooling

Scope:

- read-only discovery;
- idempotent bootstrap command;
- dry-run;
- safe aggregates;
- production-derived rehearsal.

Exclusions:

- automatic production execution;
- destructive remediation.

### PR 7.1B3: Feature-Disabled Runtime Bridge

Scope:

- trusted actor-to-primary-member resolver;
- no visible UI change;
- allowlist gate;
- compatibility tests.

### PR 7.1B4: Controlled Production Bootstrap

Scope:

- exact-image validation;
- feature-disabled deploy;
- production backup;
- dry-run;
- controlled bootstrap;
- idempotency proof;
- post-migration audit.

Exclusion: weekly menu.

## Open Implementation Decisions

| Decision | Recommendation | Alternatives | Impact before 7.1B1 |
| --- | --- | --- | --- |
| authoritative user source | `users` | union of history tables | fixes bootstrap scope |
| bootstrap mode | hybrid eager + lazy | eager only, lazy only | covers existing and future users |
| primary key format | opaque TEXT UUID | autoincrement integer | avoids exposing legacy IDs |
| owner_user_id nullability | not null for personal households | nullable owner | simpler MVP invariants |
| membership model | one active personal household per linked user | multi-household now | defer shared households |
| nutrition target bridge | use existing profile/user target source | new target table in 7.1B | minimizes data duplication |
| timezone source | nullable household default, fallback existing UTC/user config | required timezone | avoids blocking bootstrap |
| display_name storage | nullable, do not copy by default | copy username/name | minimizes PII duplication |
| batch size | bounded configurable batch | all rows in one transaction | avoids long locks |
| transaction size | per batch with idempotent resume | one global transaction | operationally safer |
| foreign keys | enable and verify per connection | assume on | required for integrity |
| partial unique indexes | use SQLite partial indexes | service-only uniqueness | stronger duplicate protection |
