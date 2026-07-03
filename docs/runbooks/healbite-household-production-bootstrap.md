# HealBite Household Production Bootstrap Runbook

Status: runbook only. Do not execute from this document without a separate controlled rollout task.

## Scope

This runbook defines the exact-image production execution contract for future Sprint 7.1B4B household rollout.

Explicit exclusions for B4A and B4A1:

```text
no production schema initialization
no production bootstrap
no production DB writes
no production config change
no build from dirty checkout
no deploy from mutable :latest namespace
no restart outside the controlled rollout playbook
no Telegram UI change
no weekly menu implementation
no shopping list implementation
no family editing implementation
```

## Exact Image Contract

All production write operations must run inside the exact deployed image namespace. Do not use the host checkout or host virtualenv for production writes.

Required execution namespace:

```bash
EXACT_CONTAINER=hermes-bot
IMAGE_ROOT=/opt/hermes
IMAGE_PYTHON=/opt/hermes/.venv/bin/python
CONTAINER_DB=/home/hermes/healbite.db
AUTH_DIR=/run/hermes-household-bootstrap-auth
ELIGIBLE_FILE=/run/hermes-household-bootstrap-eligible-users
```

Required pre-operation proof:

```text
container is running
restart_count=0
running image ref equals the immutable release tag selected for rollout
/opt/hermes/.hermes_build_sha exists inside the container
/opt/hermes/.hermes_build_sha equals the expected full lowercase 40-character Git SHA
OCI label org.opencontainers.image.revision equals the same full SHA
required scripts exist under /opt/hermes/scripts/
```

Do not use the host venv for production writes.

## Runtime Identity and SQLite Permission Gate Contract

The container starts as root so the stage2 hook can remap UID/GID and prepare writable paths, but the application runtime identity must be resolved from the exact running Hermes process inside the deployed container. `Config.User=root` is not authoritative for the application, and the static `main-hermes` s6 service is currently a no-op placeholder.

Authoritative runtime identity discovery order:

```text
1. exact running Hermes application process inside the deployed container;
2. an explicit future service-level runtime user contract, if the image changes and is revalidated;
3. Dockerfile USER or container Config.User only if separately revalidated.
```

Resolve and store exactly one runtime identity before any root-executed schema or bootstrap step:

```bash
RUNTIME_ID_LINE="$(
  docker exec "$EXACT_CONTAINER" sh -lc '
    ps -eo pid=,comm=,uid=,gid= --sort=pid |
    while read -r pid comm uid gid; do
      [ "$comm" = "hermes" ] || continue
      printf "%s %s %s\n" "$pid" "$uid" "$gid"
    done
  '
)"
RUNTIME_ID_COUNT="$(printf '%s\n' "$RUNTIME_ID_LINE" | sed '/^$/d' | wc -l)"
test "$RUNTIME_ID_COUNT" = "1"

RUNTIME_PID="$(printf '%s' "$RUNTIME_ID_LINE" | awk '{print $1}')"
RUNTIME_UID="$(printf '%s' "$RUNTIME_ID_LINE" | awk '{print $2}')"
RUNTIME_GID="$(printf '%s' "$RUNTIME_ID_LINE" | awk '{print $3}')"

test -n "$RUNTIME_PID"
test -n "$RUNTIME_UID"
test -n "$RUNTIME_GID"
```

Stop immediately if:

```text
no Hermes runtime process is present;
multiple ambiguous Hermes runtime processes are present;
runtime UID/GID cannot be determined unambiguously;
runtime UID/GID changes unexpectedly between pre-write and post-write gates.
```

Use reusable permission-gate helpers so every root-write stage is checked the same way:

```bash
DB_PARENT="$(dirname "$CONTAINER_DB")"

capture_runtime_identity() {
  local line count current_pid current_uid current_gid
  line="$(docker exec "$EXACT_CONTAINER" sh -lc '
    ps -eo pid=,comm=,uid=,gid= --sort=pid |
    while read -r pid comm uid gid; do
      [ "$comm" = "hermes" ] || continue
      printf "%s %s %s\n" "$pid" "$uid" "$gid"
    done
  ')"
  count="$(printf '%s\n' "$line" | sed '/^$/d' | wc -l)"
  test "$count" = "1"
  current_pid="$(printf '%s' "$line" | awk '{print $1}')"
  current_uid="$(printf '%s' "$line" | awk '{print $2}')"
  current_gid="$(printf '%s' "$line" | awk '{print $3}')"
  test -n "$current_pid"
  test -n "$current_uid"
  test -n "$current_gid"
  if [ -n "${RUNTIME_UID:-}" ]; then
    test "$current_uid" = "$RUNTIME_UID"
    test "$current_gid" = "$RUNTIME_GID"
  fi
  RUNTIME_PID="$current_pid"
  RUNTIME_UID="$current_uid"
  RUNTIME_GID="$current_gid"
}

capture_db_permission_state() {
  docker exec --user 0 "$EXACT_CONTAINER" sh -lc '
    db="$1"
    parent="$2"
    wal="${db}-wal"
    shm="${db}-shm"
    test -f "$db"
    test -d "$parent"
    printf "db_regular_file=%s\n" true
    printf "db_owner_uid=%s\n" "$(stat -c %u "$db")"
    printf "db_owner_gid=%s\n" "$(stat -c %g "$db")"
    printf "db_mode=%s\n" "$(stat -c %a "$db")"
    printf "db_parent_owner_uid=%s\n" "$(stat -c %u "$parent")"
    printf "db_parent_owner_gid=%s\n" "$(stat -c %g "$parent")"
    printf "db_parent_mode=%s\n" "$(stat -c %a "$parent")"
    if [ -e "$wal" ]; then
      test -f "$wal"
      printf "wal_present=%s\n" true
      printf "wal_regular_file=%s\n" true
      printf "wal_owner_uid=%s\n" "$(stat -c %u "$wal")"
      printf "wal_owner_gid=%s\n" "$(stat -c %g "$wal")"
      printf "wal_mode=%s\n" "$(stat -c %a "$wal")"
    else
      printf "wal_present=%s\n" false
    fi
    if [ -e "$shm" ]; then
      test -f "$shm"
      printf "shm_present=%s\n" true
      printf "shm_regular_file=%s\n" true
      printf "shm_owner_uid=%s\n" "$(stat -c %u "$shm")"
      printf "shm_owner_gid=%s\n" "$(stat -c %g "$shm")"
      printf "shm_mode=%s\n" "$(stat -c %a "$shm")"
    else
      printf "shm_present=%s\n" false
    fi
  ' sh "$CONTAINER_DB" "$DB_PARENT"
}

verify_runtime_db_access() {
  docker exec --user "${RUNTIME_UID}:${RUNTIME_GID}" "$EXACT_CONTAINER" sh -lc '
    db="$1"
    parent="$2"
    test -r "$db"
    test -w "$db"
    test -x "$parent"
    test -w "$parent"
    printf "runtime_db_readable=%s\n" true
    printf "runtime_db_writable=%s\n" true
    printf "runtime_parent_searchable=%s\n" true
    printf "runtime_parent_writable=%s\n" true
  ' sh "$CONTAINER_DB" "$DB_PARENT"

  docker exec --user "${RUNTIME_UID}:${RUNTIME_GID}" "$EXACT_CONTAINER" "$IMAGE_PYTHON" - <<'PY'
import sqlite3
DB = "file:/home/hermes/healbite.db?mode=ro"
con = sqlite3.connect(DB, uri=True)
con.execute("PRAGMA query_only=ON")
mode = con.execute("PRAGMA journal_mode").fetchone()[0]
con.execute("PRAGMA quick_check").fetchone()
con.close()
print(f"journal_mode={mode}")
print("runtime_sqlite_ro_probe=pass")
PY
}

verify_sqlite_sidecars() {
  docker exec --user 0 "$EXACT_CONTAINER" sh -lc '
    db="$1"
    wal="${db}-wal"
    shm="${db}-shm"
    for sidecar in "$wal" "$shm"; do
      if [ -e "$sidecar" ]; then
        test -f "$sidecar"
        test ! -L "$sidecar"
        mode="$(stat -c %a "$sidecar")"
        other="${mode#??}"
        case "$other" in
          2|3|6|7) exit 1 ;;
        esac
      fi
    done
  ' sh "$CONTAINER_DB"

  docker exec --user "${RUNTIME_UID}:${RUNTIME_GID}" "$EXACT_CONTAINER" sh -lc '
    db="$1"
    wal="${db}-wal"
    shm="${db}-shm"
    for sidecar in "$wal" "$shm"; do
      if [ -e "$sidecar" ]; then
        test -r "$sidecar"
        test -w "$sidecar"
      fi
    done
    printf "sidecars_compatible=%s\n" true
  ' sh "$CONTAINER_DB"
}

run_db_permission_gate() {
  capture_runtime_identity
  capture_db_permission_state
  verify_runtime_db_access
  verify_sqlite_sidecars
  docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
    "$IMAGE_ROOT/scripts/household_db_audit.py" \
    --db "$CONTAINER_DB" \
    --json
}
```

Permission gates must run inside the exact deployed container namespace. Do not use the host venv for authoritative permission checks. The runtime-user SQLite probe must remain read-only: `mode=ro`, `PRAGMA query_only=ON`, `PRAGMA quick_check`, then close. Do not perform business writes, artificial transactions, `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `VACUUM`, or `REINDEX` as part of the permission probe.

SQLite sidecars are race-sensitive. `-wal` and `-shm` may appear or disappear while Hermes is live. Re-stat them immediately before each compatibility check. Treat `ENOENT` as `sidecar_absent`, not as a failure. The gate fails only when an existing sidecar is incompatible with the runtime UID/GID or when the runtime user can no longer create or access sidecars through the DB parent directory.

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

Required worktree checks:

```bash
WORKTREE=/home/hermes/.hermes/worktrees/<exact-worktree>
EXACT_SHA="$(git -C "$WORKTREE" rev-parse HEAD)"
IMAGE_REF="healbite-hermes:s71b4b-${EXACT_SHA:0:12}"

git -C "$WORKTREE" status --porcelain=v1
git -C "$WORKTREE" diff --check
```

Build command:

```bash
docker build \
  --build-arg HERMES_GIT_SHA="$EXACT_SHA" \
  --label org.opencontainers.image.revision="$EXACT_SHA" \
  --tag "$IMAGE_REF" \
  "$WORKTREE"
```

Required image checks:

```bash
docker run --rm --entrypoint cat "$IMAGE_REF" /opt/hermes/.hermes_build_sha
docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_REF"
docker run --rm "$IMAGE_REF" --help
```

Required validation results:

```text
revision source is available from /opt/hermes/.hermes_build_sha
baked SHA matches the exact worktree SHA
OCI revision label matches the same full SHA
bootstrap CLI imports
audit CLI imports
feature-disabled startup probe creates no household schema
feature-disabled startup probe creates no household rows
no host-only paths are required for production writes
```

## Stage 4: Deploy Feature-Disabled Image

Deploy only `hermes-bot`. Do not recreate Qdrant.

Runtime environment must remain:

```text
HEALBITE_HOUSEHOLDS_ENABLED=false
HEALBITE_HOUSEHOLDS_ALLOWLIST=
```

Deploy command:

```bash
HERMES_IMAGE="$IMAGE_REF" docker compose \
  -p hermes-agent \
  --project-directory "$WORKTREE" \
  --env-file /home/hermes/.hermes/.env \
  -f "$WORKTREE/docker-compose.yml" \
  up -d --no-build --no-deps --force-recreate hermes-bot
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

Run read-only canonical audit against production DB from inside the exact container namespace:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_db_audit.py" \
  --db "$CONTAINER_DB" \
  --json
```

Expected safe result:

```text
schema_state=not_initialized
integrity=ok
```

If schema is already canonical, do not remove it; verify counts and conflicts. If schema is partial, unexpected, or corrupt, stop.

## Stage 5A: Pre-Root-Write DB Permission Gate

Before creating schema capability S, run the permission gate from inside the exact deployed container namespace:

```bash
run_db_permission_gate
```

Required safe result:

```text
runtime_uid discovered
runtime_gid discovered
db_regular_file=true
runtime_db_readable=true
runtime_db_writable=true
runtime_parent_searchable=true
runtime_parent_writable=true
runtime_sqlite_ro_probe=pass
journal_mode classified
wal_present true/false
shm_present true/false
sidecars_compatible=true
integrity=ok
restart_count unchanged
```

If this gate fails, stop before creating schema capability S. Do not perform automatic permission repair.

## Stage 6: Schema Authorization and Initialization

Create one root-only capability:

```text
action=household_schema_initialize
```

Run one production schema initialization command from the exact container namespace:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_bootstrap.py" \
  --db "$CONTAINER_DB" \
  --initialize-schema \
  --production-authorization-file "$AUTH_DIR/<schema-capability>" \
  --json
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

## Stage 6A: Post-Schema Audit and Permission Gate

Before creating the eligibility file, rerun the canonical audit and DB permission gate:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_db_audit.py" \
  --db "$CONTAINER_DB" \
  --json

run_db_permission_gate
```

Required safe result:

```text
schema_state=canonical
integrity=ok
runtime_uid unchanged
runtime_gid unchanged
runtime_sqlite_ro_probe=pass
sidecars_compatible=true
restart_count unchanged
```

If this gate fails, stop. Do not create the eligibility file. Do not create bootstrap capability A.

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

Run read-only dry-run with eligibility file from the exact container namespace:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_bootstrap.py" \
  --db "$CONTAINER_DB" \
  --eligible-users-file "$ELIGIBLE_FILE" \
  --json
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

Run exactly one apply from the exact container namespace:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_bootstrap.py" \
  --db "$CONTAINER_DB" \
  --apply \
  --eligible-users-file "$ELIGIBLE_FILE" \
  --production-authorization-file "$AUTH_DIR/<apply-capability-a>" \
  --json
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

## Stage 9A: Post-First Audit and Permission Gate

Before creating bootstrap capability B, rerun the canonical audit and DB permission gate:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_db_audit.py" \
  --db "$CONTAINER_DB" \
  --eligible-users-file "$ELIGIBLE_FILE" \
  --json

run_db_permission_gate
```

Required safe result:

```text
schema_state=canonical
integrity=ok
runtime_uid unchanged
runtime_gid unchanged
runtime_sqlite_ro_probe=pass
sidecars_compatible=true
restart_count unchanged
```

If this gate fails, stop. Do not create bootstrap capability B. Do not perform the second apply.

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

Run from the exact container namespace:

```bash
docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \
  "$IMAGE_ROOT/scripts/household_db_audit.py" \
  --db "$CONTAINER_DB" \
  --eligible-users-file "$ELIGIBLE_FILE" \
  --json
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

## Stage 11A: Post-Second Permission Gate

Before feature-disabled runtime proof, rerun the DB permission gate:

```bash
run_db_permission_gate
```

Required safe result:

```text
runtime_uid unchanged
runtime_gid unchanged
runtime_sqlite_ro_probe=pass
sidecars_compatible=true
integrity=ok
created_delta=0 for second apply
restart_count unchanged
```

If this gate fails, stop before feature-disabled runtime proof or stability checks.

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
sqlite permission error count=0
```

## Stage 14A: Final Stability Permission Gate

At the end of the stability window, rerun the DB permission gate and a limited recent-log review:

```bash
run_db_permission_gate
```

Required safe result:

```text
runtime_uid unchanged
runtime_gid unchanged
runtime_db_readable=true
runtime_db_writable=true
runtime_parent_searchable=true
runtime_parent_writable=true
runtime_sqlite_ro_probe=pass
sidecars_compatible=true
integrity=ok
restart_count unchanged
qdrant unchanged
sqlite_permission_error_count=0
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
runtime UID/GID cannot be determined
runtime UID/GID changed unexpectedly
DB became unreadable to the runtime user
DB became unwritable to the runtime user
DB parent directory became unsearchable or unwritable to the runtime user
WAL or SHM became root-only or otherwise runtime-incompatible
DB, WAL, or SHM became world-writable
journal mode changed unexpectedly
runtime SQLite read-only probe failed
sqlite permission error count increased
```

## SQLite Permission Remediation Policy

Do not perform automatic permission repair during the normal production flow. In particular, do not run:

```text
chown -R
chmod -R
chmod 777
chmod 666
```

If the gate detects a permission mismatch:

```text
STOP
preserve safe evidence only
do not create the next capability
do not continue schema/bootstrap writes
do not change owner/group/mode automatically
identify the exact incompatible DB or sidecar path
obtain explicit operator approval for any narrow remediation
rerun the audit and permission gate after the approved fix
```

## SQLite Permission Evidence Contract

Safe evidence fields may include only:

```text
runtime_uid
runtime_gid
db_owner_uid
db_owner_gid
db_mode
db_parent_owner_uid
db_parent_owner_gid
db_parent_mode
journal_mode
wal_present
shm_present
wal_mode
shm_mode
runtime_db_readable
runtime_db_writable
runtime_parent_searchable
runtime_parent_writable
runtime_sqlite_ro_probe
sidecars_compatible
integrity
restart_count
gate_result
sqlite_permission_error_count
```

Never include:

```text
DB inode/device
HealBite actor IDs
Telegram IDs
household/member UUIDs
profile values
meal data
weight data
water data
raw rows
raw logs
raw exception bodies
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
HOUSEHOLD PRODUCTION BOOTSTRAP COMPLETE - FEATURE DISABLED - EXISTING TELEGRAM UI UNCHANGED
```
