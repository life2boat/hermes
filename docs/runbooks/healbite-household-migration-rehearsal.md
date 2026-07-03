# HealBite Household Migration Rehearsal Runbook

Status: design-only runbook

## Scope

This runbook defines a future rehearsal process. Sprint 7.1B0 does not create a
migration script, SQL file, initializer, production DB write, build, deploy, or
restart.

## Source

Use an online SQLite backup of production:

```text
/home/hermes/healbite.db -> isolated rehearsal copy
```

Never run the first migration execution against live production DB.

## Rehearsal Sequence

1. create online SQLite backup;
2. verify backup integrity;
3. record schema fingerprint before;
4. record safe table counts before;
5. discover authoritative eligible user count from `users`;
6. validate source-user set by aggregates only;
7. run additive schema initializer on rehearsal copy;
8. verify schema and indexes;
9. run bootstrap dry-run and compare `created + already_existing = eligible`;
10. run bootstrap first pass;
11. run integrity check;
12. record household count;
13. record member count;
14. record orphan count;
15. record duplicate count;
16. record owner pointer mismatch count;
17. run initializer second pass;
18. run bootstrap second pass;
19. compare idempotency aggregates;
20. verify non-household table counts preserved;
21. compare schema fingerprint after expected additive changes;
22. run privacy scan of generated logs/evidence;
23. produce GO/NO-GO rehearsal report.

## Expected Count Invariants

Let eligible application user count be `N`.

After first bootstrap:

```text
households_created + households_already_existing = N
primary_members_created + primary_members_already_existing = N
active_owner_members=N
duplicate_linked_users=0
households_without_owner=0
owner_pointer_mismatches=0
orphan_members=0
```

After second bootstrap:

```text
households_created_delta=0
members_created_delta=0
existing_rows_mutated_unexpectedly=0
```

Do not hardcode production `N` in documentation or reports.

## Batch and Transaction Plan

Recommended defaults for future implementation planning:

```text
batch_size=100
transaction_scope=per_batch
busy_timeout=configured
resume=derive from already bootstrapped unique linked users
failure=stop current batch and report aggregate error type
```

Partial batch behavior:

- committed prior batches remain;
- failing batch rolls back;
- rerun is idempotent;
- no destructive cleanup is attempted automatically.

## Safe Rehearsal Aggregates

Allowed:

```text
eligible_user_count
created_household_count
created_member_count
already_bootstrapped_count
invalid_actor_count
duplicate_conflict_count
orphan_count
households_without_owner_count
owner_pointer_mismatch_count
duration_bucket
error_type
```

Forbidden:

```text
user IDs
Telegram IDs
names
profile contents
nutrition values
chat IDs
raw SQL rows
raw exception bodies
```

## Future Read-Only Audit CLI Contract

Future tool name:

```text
scripts/household_db_audit.py
```

It should return safe aggregates only:

```text
schema_state
households_total
active_households
members_total
active_members
owner_members
linked_members
unlinked_members
households_without_owner
owner_pointer_mismatches
duplicate_active_linked_users
invalid_actor_count
orphan_members
integrity
```

Do not create the CLI in Sprint 7.1B0.

## Failure Handling

Stop and report safe aggregates on:

- integrity check failure;
- foreign keys disabled;
- duplicate active linked user;
- two active owners;
- household without owner;
- owner pointer mismatch;
- orphan member;
- disk full;
- DB locked beyond retry budget;
- schema mismatch;
- unexpected non-household table count changes.

Do not automatically merge households. Do not drop additive tables. Do not
restore production DB unless a later controlled production task proves
corruption or data loss and explicitly authorizes restore.

## Rollout Preconditions for Future Production Bootstrap

- exact image validated;
- production DB backup created;
- rehearsal GO on production-derived copy;
- feature disabled;
- allowlist empty unless explicit rollout says otherwise;
- dry-run aggregate proves `created + already_existing = eligible`;
- operator approves controlled bootstrap;
- rollback plan documented;
- post-bootstrap audit available.

## Success Verdict

Future rehearsal may report GO only if:

```text
integrity=ok
households_created + households_already_existing = N
primary_members_created + primary_members_already_existing = N
duplicate_linked_users=0
households_without_owner=0
owner_pointer_mismatches=0
orphan_members=0
second_run_delta=0
non_household_counts_preserved=true
privacy_scan=pass
```

## Explicit Exclusions

```text
no runtime code
no schema migration
no production backfill
no Telegram UI
no weekly menu
no shopping list
no family editing
no production DB writes
no build
no deploy
no restart
```

## Sprint 7.1B2 Implemented Tooling

Status: tooling implemented for rehearsal and audit only. Do not run `--apply`
against `/home/hermes/healbite.db`.

### Read-Only Audit

```bash
python scripts/household_db_audit.py --db /path/to/healbite-copy.db --json
```

The audit opens SQLite in read-only mode and reports safe aggregates only. It
never initializes schema and never creates a missing DB file.

Key output fields:

```text
schema_state
integrity
identity_column
eligibility_state
households_total
members_total
owner_pointer_mismatches
duplicate_active_linked_users
households_without_owner
orphan_members
invalid_uuid
invalid_version
invalid_enum
eligible_users_total
eligible_users_covered
eligible_users_missing
```

### Bootstrap Dry-Run

Default mode is dry-run:

```bash
python scripts/household_bootstrap.py --db /path/to/healbite-copy.db --json
```

Dry-run does not create schema, does not create a missing DB, does not start a
write transaction, and does not modify household rows.

### Schema Initialization On Rehearsal Copy

Schema initialization is explicit and must be run only on an isolated copy:

```bash
python scripts/household_bootstrap.py \
  --db /path/to/healbite-copy.db \
  --initialize-schema \
  --dry-run \
  --json
```

### Eligibility Policy

If `users` does not contain reliable bot/system/test metadata, `--apply` is
refused unless an operator-provided eligibility file is supplied:

```bash
python scripts/household_bootstrap.py \
  --db /path/to/healbite-copy.db \
  --apply \
  --eligible-users-file /run/operator-approved-household-users \
  --json
```

The eligibility file contains one positive integer application user ID per line.
It is an allowlist over authoritative `users`, not an alternate identity source.
Never commit the file, copy it into evidence, or print its contents.

### Production Path Guard

`--apply --db /home/hermes/healbite.db` is guarded and refused in Sprint 7.1B2.
Production bootstrap requires a later controlled production task.

### Exit Codes

```text
0 = success or dry-run completed
2 = invalid arguments
3 = DB unavailable or missing
4 = schema not canonical / not initialized
5 = integrity failure
6 = eligibility policy missing or invalid
7 = household conflict or partial state
8 = apply execution failure
```

### Privacy Contract

Human and JSON output must contain aggregates only. Forbidden output includes:

```text
user IDs
Telegram IDs
household UUIDs
member UUIDs
names
raw SQL rows
eligibility-file contents
raw exception bodies
```

### Explicit Exclusions For Sprint 7.1B2

```text
no live production bootstrap
no production schema initialization
no Telegram UI
no callback changes
no runtime startup wiring
no weekly menu
no shopping list
no family editing
no LLM changes
no build
no deploy
no restart
```
