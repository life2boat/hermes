# HealBite Household Production Bootstrap Runbook

Status: runbook only. Do not execute from this document without a separate controlled rollout task.

## Scope

This runbook covers the future Sprint 7.1B4B exact-image deployment and controlled production household schema/bootstrap. Sprint 7.1B4A only adds the one-time authorization mechanism and this runbook.

Explicit exclusions for B4A:

```text
no production schema initialization
no production bootstrap
no production DB writes
no production config change
no build
no deploy
no restart
no Telegram UI change
no weekly menu implementation
no shopping list implementation
no family editing implementation
```

## Authorization Model

Production write operations remain denied by default. The bootstrap CLI accepts `--production-authorization-file PATH` only for production DB write operations and only when the capability file authorizes exactly one action:

```text
household_schema_initialize
household_bootstrap_apply
```

A schema capability cannot authorize bootstrap apply. A bootstrap capability cannot authorize schema initialization. Combined production writes in one CLI invocation are refused.

Capability files must be created outside the repository, under:

```text
/run/hermes-household-bootstrap-auth/
```

Directory requirements:

```text
owner=root
mode=0700
```

Capability file requirements:

```text
regular file
not symlink
not hardlink
mode=0600
owner=root or trusted current operator user
strict JSON schema version 1
no unknown fields
expires within 15 minutes
nonce has at least 128 bits of entropy
bound to production DB realpath, device, inode
bound to exact running image revision
```

Never commit, back up, print, or log capability content, nonce, DB inode/device, user IDs, Telegram IDs, household IDs, or member IDs.

## Capability JSON Shape

Schema initialization:

```json
{
  "schema_version": 1,
  "action": "household_schema_initialize",
  "database_realpath": "/home/hermes/healbite.db",
  "database_device": 0,
  "database_inode": 0,
  "expected_revision": "FULL_40_CHARACTER_LOWERCASE_GIT_SHA",
  "issued_at_utc": "2026-07-03T00:00:00Z",
  "expires_at_utc": "2026-07-03T00:15:00Z",
  "nonce": "HIGH_ENTROPY_RANDOM_VALUE"
}
```

Bootstrap apply uses the same shape with:

```text
action=household_bootstrap_apply
```

## Stage 1: Baseline

Collect safe aggregates only:

```text
current hermes container id
current hermes image id
current image revision
hermes restart count
qdrant container id
qdrant image id
qdrant restart count
production DB path
production DB integrity
root filesystem free space
household feature enabled=false
household allowlist count=0
household audit schema_state
```

Expected before bootstrap:

```text
feature=false
allowlist_count=0
schema_state=not_initialized
integrity=ok
```

If production baseline changes unexpectedly, stop.

## Stage 2: Predeploy Backup

Create evidence directory:

```text
/home/hermes/backups/s71b4_household_production_bootstrap/<UTC_TIMESTAMP>
```

Permissions:

```text
directory=0700
files=0600
```

Create online SQLite backup through the SQLite backup API:

```text
healbite-pre-household-bootstrap.db
```

Record only safe aggregates and checksums. Do not copy capability files. Do not include raw logs or user data.

## Stage 3: Exact Image Build and Validation

Build from a clean detached worktree at the merged B4A main SHA.

Required host checks before image build:

```text
household production auth tests
household bootstrap tests
household audit tests
household core tests
household runtime bridge tests
adjacent HealBite tests
scripts/agent_check.sh
git diff --check
```

Required image checks:

```text
revision source is available from /opt/hermes/.hermes_build_sha
bootstrap CLI imports
bootstrap help runs
audit CLI imports
audit help runs
feature-disabled startup probe creates no household schema
feature-disabled startup probe creates no household rows
```

## Stage 4: Deploy Feature-Disabled Image

Deploy only `hermes-bot`. Do not recreate Qdrant.

Runtime environment must remain:

```text
HEALBITE_HOUSEHOLDS_ENABLED=false
HEALBITE_HOUSEHOLDS_ALLOWLIST=
```

After deploy verify:

```text
exact image running
restart_count=0
qdrant unchanged
feature=false
allowlist_count=0
startup did not initialize household schema
startup did not create household rows
existing HealBite features healthy
```

## Stage 5: Pre-Initialization Audit

Run read-only canonical audit against production DB:

```bash
python scripts/household_db_audit.py --db /home/hermes/healbite.db --json
```

Expected safe result:

```text
schema_state=not_initialized
integrity=ok
```

If schema is already canonical, do not remove it; verify counts and conflicts. If schema is partial, unexpected, or corrupt, stop.

## Stage 6: Schema Authorization and Initialization

Create one root-only capability:

```text
action=household_schema_initialize
```

Run one production schema initialization command:

```bash
python scripts/household_bootstrap.py   --db /home/hermes/healbite.db   --initialize-schema   --production-authorization-file /run/hermes-household-bootstrap-auth/<schema-capability>   --json
```

Verify:

```text
capability consumed
schema_state=canonical
households_total unchanged or 0
members_total unchanged or 0
integrity=ok
feature=false
allowlist_count=0
```

## Stage 7: Eligibility Policy

Create protected eligibility file:

```text
/run/hermes-household-bootstrap-eligible-users
```

Requirements:

```text
owner=root
mode=0600
regular file
not symlink
one positive application user ID per line
```

The operator must derive the set only from authoritative production users and exclude known system, bot, and test actors. Do not print IDs in evidence or reports.

## Stage 8: Production Dry Run

Run read-only dry-run with eligibility file:

```bash
python scripts/household_bootstrap.py   --db /home/hermes/healbite.db   --eligible-users-file /run/hermes-household-bootstrap-eligible-users   --json
```

Required safe result:

```text
integrity=ok
schema_state=canonical
eligibility_state=verified
conflict_count=0
partial_count=0
apply_ready=true
would_create_count + already_existing_count = eligible_count
```

If conflicts, partial state, invalid eligibility, or integrity failure appears, stop before creating bootstrap capability.

## Stage 9: First Bootstrap Apply

Create a new root-only capability:

```text
action=household_bootstrap_apply
```

Run exactly one apply:

```bash
python scripts/household_bootstrap.py   --db /home/hermes/healbite.db   --apply   --eligible-users-file /run/hermes-household-bootstrap-eligible-users   --production-authorization-file /run/hermes-household-bootstrap-auth/<apply-capability-a>   --json
```

Verify:

```text
capability consumed
created_count + already_existing_count = eligible_count
owner_pointer_mismatches=0
duplicate_active_linked_users=0
households_without_owner=0
orphan_members=0
invalid_uuid=0
invalid_version=0
invalid_enum=0
integrity=ok
```

## Stage 10: Second Bootstrap Apply

Create a second, distinct bootstrap capability. Do not reuse the first capability.

Run the same apply command with capability B.

Required result:

```text
capability consumed
created_count=0
already_existing_count=eligible_count
household_count unchanged
member_count unchanged
semantic_delta=0
integrity=ok
```

## Stage 11: Canonical Audit

Run:

```bash
python scripts/household_db_audit.py   --db /home/hermes/healbite.db   --eligible-users-file /run/hermes-household-bootstrap-eligible-users   --json
```

Required result:

```text
schema_state=canonical
integrity=ok
eligibility_state=verified
eligible_users_missing=0
owner_pointer_mismatches=0
duplicate_active_linked_users=0
households_without_owner=0
orphan_members=0
invalid_uuid=0
invalid_version=0
invalid_enum=0
```

## Stage 12: Feature-Disabled Runtime Proof

The runtime must remain feature-disabled:

```text
feature=false
allowlist_count=0
```

Verify household row deltas remain zero for safe existing routes:

```text
/start
main menu
profile read
food diary read
weight read
water read
weekly report read
weekly menu placeholder
shopping list placeholder
family placeholder
```

Do not invoke internal household create from production Telegram paths.

## Stage 13: Existing Feature Smoke

Verify existing features without unnecessary user-data mutation:

```text
Telegram polling
main menu
profile read
food diary read
weight read
water read
weekly report read
reminder feature remains disabled
```

## Stage 14: Stability Window

Wait at least 10 minutes and verify:

```text
Hermes running
restart_count=0
Qdrant unchanged
DB integrity=ok
household audit canonical
household counts unchanged
feature=false
allowlist_count=0
no household runtime create markers
no privacy violations
root free space safe
```

## Stop Triggers

Stop all further writes if any of these occur:

```text
revision mismatch
DB identity mismatch
capability validation failure
capability replay
schema partial/unexpected
integrity failure
eligibility mismatch
household conflicts
owner pointer mismatch
duplicate linked users
orphan members
non-household count change
container restart
Qdrant change
privacy leak
```

## Rollback Policy

If the image is unhealthy, roll back only the `hermes-bot` container to the previous image while keeping:

```text
feature=false
allowlist empty
```

If household schema and bootstrap rows are correct but runtime image is rolled back, additive household tables and rows may remain. Older runtime does not depend on them.

Do not run `DROP TABLE`. Do not delete bootstrap rows automatically. Restore production DB only for proven corruption or non-household data loss and only with explicit operator approval.

## Privacy Contract

Reports and evidence may contain only counts and booleans. Never include:

```text
user IDs
Telegram IDs
eligibility file contents
household IDs
member IDs
capability nonce
capability JSON
capability file path
DB inode/device
profile values
meal data
weight data
water data
raw exception bodies
```

## Final B4B Verdict Template

```text
HOUSEHOLD PRODUCTION BOOTSTRAP COMPLETE ? FEATURE DISABLED ? EXISTING TELEGRAM UI UNCHANGED
```
