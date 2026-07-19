# HealBite Weekly Menu and Shopping Feature-Disabled Production Rollout

Status: runbook only. Do not execute from this document without a separate controlled rollout task.

## Scope

This runbook defines the future exact-image rollout contract for the additive weekly-menu and shopping schema/runtime stack that exists in `main` as of `31f2594d2de352db3c0c6c78513770bdf5c606ab`.

This document is a D0 readiness artifact only.

It does not authorize:

```text
production SSH actions from this task
production DB opens from this task
production backups from this task
schema initialization from this task
feature enablement
allowlist population
Telegram mutation UI rollout
Docker build execution from this task
deploy or restart from this task
Qdrant changes
provider calls
```

## Status Helper Contract

Operational status collection must stay strictly read-only.

```text
./scripts/healbite status must never run write probes
./scripts/healbite status must never create schemas, rows, WAL, SHM, or journal files
status DB inspection must open SQLite with mode=ro and PRAGMA query_only=ON
status may be run against a local copied DB via ./scripts/healbite status --db-path <path>
status provider markers are classification counters only; status must not make provider or generation calls
```

Provider marker interpretation for rollout evidence:

```text
provider_calls = actual provider calls attempted during the inspected operation
provider_call_failures = actual provider calls that failed
provider_auth_failures = provider call failures caused by auth/config credentials
provider_unavailable_without_call = provider unavailable state inferred without a real call
provider_not_configured = no usable provider configuration detected
generation_calls = actual content-generation calls attempted during the inspected operation
```

## Current Production Baseline

Document and re-check these facts during the future execution stage:

```text
deployed source revision = 04566a0dd2b79f60748194cc3d318c5a5e75f3d3
deployed image tag = healbite-hermes:s71b4b-04566a0dd2b7
deployed image ID = sha256:a7f761eea1c19a3c2552eaad691ee49ef278817a2d3c51a1d1a858be1a5a9ee7
households = 4
household_members = 4
household feature = disabled
weekly-menu schema = not initialized
shopping schema = not initialized
weekly-menu feature = not deployed
shopping feature = not deployed
weekly allowlist = not configured
shopping allowlist = not configured
reminders = disabled
Telegram placeholders remain safe:
  📋 Меню на неделю -> В разработке
  🛒 Список покупок -> В разработке
  👨‍👩‍👧 Семья -> В разработке
```

## Current Main Baseline

Authorized source SHA for the future rollout planning sequence:

```text
31f2594d2de352db3c0c6c78513770bdf5c606ab
```

That main baseline already contains:

```text
household foundation
production household bootstrap
weekly-menu schema/store
shopping schema/store
feature-disabled weekly runtime
feature-disabled shopping runtime
explicit runtime resource lifetime
read-only weekly Telegram UI
owner-only weekly mutations
validated draft generation
profile snapshot hardening
```

Production remains behind that baseline. This runbook separates future rollout into D1-D5 and keeps weekly/shopping features disabled until a later explicit canary.

## Exact Source and Image Contract

Future production rollout must run from one exact approved source SHA only.

Required execution variables:

```bash
set -euo pipefail

EXACT_SHA="31f2594d2de352db3c0c6c78513770bdf5c606ab"
BRANCH="healbite-main"
WORKTREE="/home/hermes/.hermes/worktrees/${BRANCH}"
COMPOSE_FILE="$WORKTREE/docker-compose.yml"
ENV_FILE="/home/hermes/.hermes/.env"
COMPOSE=(docker compose -p hermes-agent --project-directory "$WORKTREE" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

HERMES_SERVICE="hermes-bot"
QDRANT_SERVICE="qdrant"
IMAGE_ROOT="/opt/hermes"
IMAGE_PYTHON="/opt/hermes/.venv/bin/python"
CONTAINER_DB="/home/hermes/healbite.db"
BUILD_SHA_FILE="/opt/hermes/.hermes_build_sha"
IMAGE_REF="healbite-hermes:s71d1-${EXACT_SHA:0:12}"
```

Required build contract:

```text
exact SHA only
clean exact-SHA worktree only
no build from dirty canonical checkout
no floating latest tag as evidence
record full 40-character SHA separately from short tag
record image ID after build
record OCI revision label after build
record inside-image .hermes_build_sha after build
```

Exact build proof commands for the future D1 stage:

```bash
cd "$WORKTREE"
test "$(git rev-parse HEAD)" = "$EXACT_SHA"
test -z "$(git status --porcelain=v1)"
git diff --check

HERMES_GIT_SHA="$EXACT_SHA" HERMES_IMAGE="$IMAGE_REF" \
  "${COMPOSE[@]}" build hermes-bot

BUILT_IMAGE_ID="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')"
IMAGE_REVISION_LABEL="$(docker image inspect "$IMAGE_REF" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
test "$IMAGE_REVISION_LABEL" = "$EXACT_SHA"

IMAGE_BUILD_SHA="$(docker run --rm --entrypoint cat "$IMAGE_REF" "$BUILD_SHA_FILE")"
test "$IMAGE_BUILD_SHA" = "$EXACT_SHA"
```

## Runtime and Service Identity Contract

Canonical service identities from the current compose configuration:

```text
Hermes service name = hermes-bot
Qdrant service name = qdrant
Hermes compose image variable = HERMES_IMAGE
Hermes compose build arg = HERMES_GIT_SHA
Hermes compose command = ["hermes", "gateway"]
Hermes host env file = /home/hermes/.hermes/.env
Hermes host DB mount = /home/hermes/healbite.db
Hermes container DB path = /home/hermes/healbite.db
Hermes HOME/HERMES_HOME = /home/hermes and /home/hermes/.hermes
Image Python path = /opt/hermes/.venv/bin/python
Build SHA file = /opt/hermes/.hermes_build_sha
Runtime default UID = 10000
Runtime UID/GID may be remapped only through HERMES_UID/HERMES_GID or PUID/PGID
Dockerfile ENTRYPOINT = /init /opt/hermes/docker/main-wrapper.sh
Compose runtime command = hermes gateway
Container healthcheck = none; health must be established from running status, restart count, logs, and Telegram smoke
```

Service isolation requirements:

```text
only hermes-bot may be recreated
Qdrant must remain unchanged
docker compose down is prohibited
volume removal is prohibited
docker system prune is prohibited
```

Qdrant identity must remain constant before and after a future Hermes rollout:

```bash
OLD_QDRANT_CONTAINER_ID="$(docker inspect -f '{{.Id}}' "$QDRANT_SERVICE")"
OLD_QDRANT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$QDRANT_SERVICE")"
OLD_QDRANT_CREATED="$(docker inspect -f '{{.Created}}' "$QDRANT_SERVICE")"

# ... after Hermes-only recreation ...

test "$(docker inspect -f '{{.Id}}' "$QDRANT_SERVICE")" = "$OLD_QDRANT_CONTAINER_ID"
test "$(docker inspect -f '{{.Image}}' "$QDRANT_SERVICE")" = "$OLD_QDRANT_IMAGE_ID"
test "$(docker inspect -f '{{.Created}}' "$QDRANT_SERVICE")" = "$OLD_QDRANT_CREATED"
```

## Feature-Disabled Environment Contract

The future D2 image deployment must remain fail-closed:

```text
HEALBITE_WEEKLY_MENU_ENABLED=false
HEALBITE_WEEKLY_MENU_ALLOWLIST empty
HEALBITE_SHOPPING_LIST_ENABLED=false
HEALBITE_SHOPPING_LIST_ALLOWLIST empty
```

The canonical parser behavior is:

```text
absent enabled variable -> disabled
blank enabled variable -> disabled
absent allowlist variable -> empty allowlist
blank allowlist variable -> empty allowlist
malformed enabled/allowlist variable -> misconfigured fail-closed state
```

For production rollout evidence, prefer explicit disabled booleans and explicit empty allowlists in a temporary override file:

```yaml
services:
  hermes-bot:
    environment:
      HEALBITE_WEEKLY_MENU_ENABLED: "false"
      HEALBITE_WEEKLY_MENU_ALLOWLIST: ""
      HEALBITE_SHOPPING_LIST_ENABLED: "false"
      HEALBITE_SHOPPING_LIST_ALLOWLIST: ""
```

Feature-disabled rollout must not introduce:

```text
real allowlist values
family UI enablement
weekly mutation UI enablement
shopping UI enablement
background generation
provider startup dependency
schema auto-initialization
business-row auto-creation
```

## Startup Side-Effect Contract

Current main must remain safe when weekly and shopping schema are absent and both features are disabled:

```text
gateway/run.py does not call weekly initialize_schema()
gateway/run.py does not call shopping initialize_schema()
feature-disabled weekly runtime checks gate -> household auth -> store/schema read only
feature-disabled shopping runtime checks gate -> household auth -> store/schema read only
Telegram placeholders remain fail closed outside future allowlists
missing weekly/shopping schema must not break disabled startup
```

The future D2 rollout must prove:

```text
Hermes startup succeeds
weekly schema remains not_initialized before D3
shopping schema remains dependency_missing or not_initialized before D3
no weekly/shopping DDL occurs at startup
no weekly or shopping business rows appear at startup
no provider calls occur at startup
no household bootstrap rerun occurs automatically
```

## D1 - Exact Image Build and Offline Validation

This stage is future-only. Do not execute it from the D0 task.

### D1 Pre-build checks

```bash
set -euo pipefail

cd "$WORKTREE"
test "$(git rev-parse HEAD)" = "$EXACT_SHA"
test -z "$(git status --porcelain=v1)"
git diff --check

PY="/home/hermes/.hermes/hermes-agent/venv/bin/python"
test -x "$PY"

TMPDIR=/tmp "$PY" -m pytest -q \
  tests/gateway/test_healbite_feature_gates.py \
  tests/gateway/test_healbite_runtime_resources.py \
  tests/gateway/test_healbite_weekly_menu_schema.py \
  tests/gateway/test_healbite_weekly_menus.py \
  tests/gateway/test_healbite_weekly_menu_runtime.py \
  tests/gateway/test_healbite_weekly_menu_telegram.py \
  tests/gateway/test_healbite_weekly_menu_mutation_runtime.py \
  tests/gateway/test_healbite_weekly_menu_generation.py \
  tests/gateway/test_healbite_shopping_schema.py \
  tests/gateway/test_healbite_shopping.py \
  tests/gateway/test_healbite_shopping_runtime.py \
  tests/test_exact_image_build_contract.py \
  tests/test_weekly_menu_shopping_design_contract.py \
  tests/test_weekly_menu_shopping_production_readiness_contract.py \
  -W error::RuntimeWarning

TMPDIR=/tmp bash scripts/agent_check.sh
"/home/hermes/.hermes/hermes-agent/venv/bin/python" scripts/check-windows-footguns.py --all
```

### D1 Build and exact-image proof

```bash
set -euo pipefail

cd "$WORKTREE"
HERMES_GIT_SHA="$EXACT_SHA" HERMES_IMAGE="$IMAGE_REF" \
  "${COMPOSE[@]}" build hermes-bot

BUILT_IMAGE_ID="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')"
IMAGE_REVISION_LABEL="$(docker image inspect "$IMAGE_REF" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
IMAGE_BUILD_SHA="$(docker run --rm --entrypoint cat "$IMAGE_REF" "$BUILD_SHA_FILE")"

test "$IMAGE_REVISION_LABEL" = "$EXACT_SHA"
test "$IMAGE_BUILD_SHA" = "$EXACT_SHA"
test -n "$BUILT_IMAGE_ID"
```

## D2 - Feature-Disabled Hermes-Only Deployment

This stage is future-only. It does not initialize schema and does not enable features.

### D2 Preflight

```bash
set -euo pipefail

EXACT_CONTAINER="$HERMES_SERVICE"
EXPECTED_OLD_IMAGE_TAG="healbite-hermes:s71b4b-04566a0dd2b7"
EXPECTED_OLD_IMAGE_ID="sha256:a7f761eea1c19a3c2552eaad691ee49ef278817a2d3c51a1d1a858be1a5a9ee7"

OLD_HERMES_CONTAINER_ID="$(docker inspect -f '{{.Id}}' "$EXACT_CONTAINER")"
OLD_HERMES_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$EXACT_CONTAINER")"
OLD_HERMES_STATUS="$(docker inspect -f '{{.State.Status}}' "$EXACT_CONTAINER")"
OLD_HERMES_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "$EXACT_CONTAINER")"

OLD_QDRANT_CONTAINER_ID="$(docker inspect -f '{{.Id}}' "$QDRANT_SERVICE")"
OLD_QDRANT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$QDRANT_SERVICE")"
OLD_QDRANT_STATUS="$(docker inspect -f '{{.State.Status}}' "$QDRANT_SERVICE")"

test "$OLD_HERMES_STATUS" = "running"
test "$OLD_HERMES_RESTARTS" = "0"
test "$OLD_QDRANT_STATUS" = "running"
test -n "$OLD_QDRANT_CONTAINER_ID"
test -n "$OLD_QDRANT_IMAGE_ID"

docker system df
df -h /home/hermes
```

### D2 Temporary feature-disabled override

Create a temporary override file outside Git. Do not put real allowlist values into this file.

```bash
set -euo pipefail

OVERRIDE_FILE="/run/hermes-s71d1-feature-disabled.yml"
cat > "$OVERRIDE_FILE" <<'YAML'
services:
  hermes-bot:
    image: ${HERMES_IMAGE}
    environment:
      HEALBITE_WEEKLY_MENU_ENABLED: "false"
      HEALBITE_WEEKLY_MENU_ALLOWLIST: ""
      HEALBITE_SHOPPING_LIST_ENABLED: "false"
      HEALBITE_SHOPPING_LIST_ALLOWLIST: ""
YAML
chmod 600 "$OVERRIDE_FILE"
```

### D2 Hermes-only recreate

```bash
set -euo pipefail

HERMES_IMAGE="$IMAGE_REF" \
  "${COMPOSE[@]}" -f "$OVERRIDE_FILE" up -d --no-deps --force-recreate hermes-bot
```

### D2 Post-deploy checks

```bash
set -euo pipefail

NEW_HERMES_CONTAINER_ID="$(docker inspect -f '{{.Id}}' "$EXACT_CONTAINER")"
NEW_HERMES_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$EXACT_CONTAINER")"
NEW_HERMES_STATUS="$(docker inspect -f '{{.State.Status}}' "$EXACT_CONTAINER")"
NEW_HERMES_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "$EXACT_CONTAINER")"

test "$NEW_HERMES_STATUS" = "running"
test "$NEW_HERMES_RESTARTS" = "0"
test "$NEW_HERMES_IMAGE_ID" = "$BUILT_IMAGE_ID"
test "$NEW_HERMES_CONTAINER_ID" != "$OLD_HERMES_CONTAINER_ID"

test "$(docker inspect -f '{{.Id}}' "$QDRANT_SERVICE")" = "$OLD_QDRANT_CONTAINER_ID"
test "$(docker inspect -f '{{.Image}}' "$QDRANT_SERVICE")" = "$OLD_QDRANT_IMAGE_ID"

test "$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$NEW_HERMES_IMAGE_ID")" = "$EXPECTED_MAIN_SHA"
docker logs --tail 200 "$EXACT_CONTAINER" 2>&1 | egrep -i "traceback|database is locked|readonly|provider authentication|command approval" || true
```

D2 stop conditions:

```text
Hermes unhealthy
restart_count != 0
Qdrant changed
weekly or shopping schema auto-created
feature unexpectedly enabled
allowlist unexpectedly nonempty
existing Telegram baseline regressed
```

## Backup Contract Before First Production DDL

The first production DDL is D3 weekly schema initialization.

Before any D3 step:

```text
backup path must be outside the live DB directory
backup filename must be timestamped and immutable
backup must use SQLite backup API or an existing approved equivalent
backup must record SHA-256
backup copy must pass integrity_check
restore test must target a separate temporary path
backup must never be committed or uploaded
backup contents must never appear in reports
```

Approved future backup pattern:

```bash
set -euo pipefail

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/home/hermes/backups/s71d3-weekly-shopping/$TS"
BACKUP_DB="$BACKUP_DIR/healbite.db"
RESTORE_DB="/tmp/healbite-restore-$TS.db"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

sqlite3 -cmd '.timeout 5000' /home/hermes/healbite.db ".backup '$BACKUP_DB'"

sha256sum "$BACKUP_DB" > "$BACKUP_DB.sha256"
sqlite3 "$BACKUP_DB" 'PRAGMA integrity_check;'
sqlite3 "$BACKUP_DB" '.backup '"'"'"$RESTORE_DB"'"'"''
sqlite3 "$RESTORE_DB" 'PRAGMA integrity_check;'
rm -f "$RESTORE_DB"
```

Replace the placeholder backup path before execution. Do not use a plain `cp` of the active SQLite database unless separate quiescence proof exists.

## D3 - Explicit Weekly Then Shopping Schema Initialization

This stage is future-only and must occur only after D2 image stability is proven.

### D3 Production DB baseline before DDL

Record only safe aggregates:

```text
integrity_check
foreign_key_check
households count
household_members count
users count
profiles count
nutrition_log count
weight-related counts
water-related counts
reminder counts
weekly schema state
shopping schema state
```

Do not print IDs or row payloads.

### D3 Weekly schema initialization

The authoritative architecture is staged copy plus atomic publish. Direct in-place
migration of the production database is prohibited. The public migration-only CLI
may receive only a disposable staging copy in a private writable directory; the
production DB and its parent must never be mounted in the migration container.

The staged-copy implementation remains in
`scripts/hermes_staged_schema_migrate.py`. The single public production
authorization entrypoint is `scripts/hermes_production_staged_migrate.py`, with
separate explicit `plan` and `execute` subcommands. The fail-closed default is
explicit: production execution is disabled by default; there is no default database
path, environment fallback,
generic confirmation flag, in-place migration, or implicit container stop.
A production execute mode is deliberately absent from the internal
`scripts/hermes_staged_schema_migrate.py` CLI; only the separate hash-bound gate
can authorize production execution. The production gate delegates the authorized
mutation to the existing staged-copy implementation; inside the isolated staging
container that implementation invokes only
`scripts/healbite_schema_migrate.py --staged-copy` from the exact image.

Required contract:

```text
plan and execute must run as root; the plan records and execute revalidates the
creator UID, GID, username, process UID, and process GID
production plan requires explicit --repository-root, --db-path, --backup-parent,
--staging-parent, --evidence-parent, --migration-image-id,
--migration-image-revision, --previous-image-id, --expected-hostname,
--expected-source-device, --expected-source-inode, --expected-source-size,
--expected-source-sha256, --expected-free-bytes, and --expires-in-seconds
production plan additionally requires explicit --operations-root-approval,
--expected-operations-root-approval-sha256, --clean-start-policy, and
--expected-clean-start-policy-sha256; none has a default or environment fallback
the operations-root approval must be root-owned canonical JSON, mode 0600 under a
root-owned mode 0700 parent; the approved root must not be group/world writable;
it must be no more than 24 hours old and bind the clean repository
path/device/inode/owner/mode/tree, exact HEAD, canonical deployment
contract identity/hash, migration entrypoint/staged implementation/runbook hashes,
and exact image ID/revision
the clean-start policy must be root-owned canonical JSON with the same descriptor
controls and must bind NO_CLIENTS_CLEAN_START, exact source SHA-256, exact main and
image, no Family/Shopping backfill, explicit legacy reset permission, preservation
of Memory OS, nutrition diary, Telegram admin configuration and out-of-scope tables,
and false execution/deletion state
both evidence files are opened with NOFOLLOW, hashed and parsed from pinned file
descriptors before production source inspection; their path, filesystem identity,
mode and SHA-256 are recorded in plan schema version 3
the only deployment authority is
<repository-root>/deploy/hermes-production.json opened with NOFOLLOW and pinned
by file descriptor; caller-selected contract paths are not accepted
Household and Shopping enabled flags must both be explicit JSON booleans false,
and both allowlists must be empty in that pinned canonical contract
the target schema version and fingerprint are derived from trusted migration
code, recorded in the plan, and independently recalculated during execute
production execute requires exact --plan, --expected-plan-sha256,
--confirm-operation-id, --confirm-source-sha256, and --confirm-image-revision
plus independent --confirm-operations-root-approval-sha256 and
--confirm-clean-start-policy-sha256 values
execute securely reopens both evidence files, confirms their recorded identities and
hashes, revalidates approval expiry/repository provenance and all policy semantics,
and pins their descriptors through authorization and staged execution
plan and execute are separate and require an independent plan/SHA review gate
no path, evidence, image, revision, confirmation, or authorization comes from
environment; no generic force or skip-validation flag exists
plan, backup, staging, and evidence parents are canonical, non-symlink,
root-owned, mode 0700 directories
the operator must stop the application and every DB user through the canonical
deployment source of truth before execute
no process may have the source DB open
no -journal, -wal, or -shm source sidecar may exist
source identity, mode, integrity, and foreign keys must match the approved plan
execute must acquire the real SQLite lifetime lease before creating its
execution directory, execution.json, backup, staging, or internal manifest
an active reader or writer must return QUIESCENCE_FAILED with zero execute
filesystem delta; the separately approved immutable plan remains the only file
automatic lock retry is prohibited
```

### D3 production plan/execute gate (future example only)

The following workflow is a deterministic **example for a separately authorized
production task**. This PR does not authorize running any command in this section.
The plan author and execute approver must be separate people or separate recorded
review steps. The reviewer approves the exact canonical plan path, plan SHA-256,
operation ID, source SHA-256, and migration image revision before service stop.

1. Verify the exact-main image ID and OCI revision through the canonical deployment
   source of truth.
2. Create one immutable production plan while Hermes remains running.
3. Independently review the plan, its mode 0600, canonical JSON, and SHA-256.
4. Stop only hermes-bot through the canonical deployment source of truth.
5. Let execute independently prove real SQLite quiescence and hold both lifetime
   leases; a generic flock or process-name check is not sufficient.
6. Execute only the exact approved plan.
7. Perform a separate read-only integrity, foreign-key, and schema verification.
8. Start the exact image through scripts/hermes_production_deploy.py with all
   manifest feature gates disabled and allowlists empty.
9. Run the approved Telegram smoke while Household/Weekly/Shopping enablement is
   unchanged.
10. Retain the plan, execution evidence, displaced source, and durable backup.
11. Do not enable Family, Weekly mutation, or Shopping in this workflow.

Example plan preparation, not current production authorization:

```bash
set -euo pipefail

HOST_PYTHON="<approved-host-python>"
GATE="scripts/hermes_production_staged_migrate.py"
REPOSITORY_ROOT="<exact-clean-repository-root>"
DB_PATH="<explicit-approved-database-path>"
BACKUP_PARENT="<explicit-private-backup-parent>"
STAGING_PARENT="<explicit-private-same-filesystem-staging-parent>"
EVIDENCE_PARENT="<explicit-private-evidence-parent>"
OPERATIONS_ROOT_APPROVAL="<reviewed-canonical-approval-path>"
OPERATIONS_ROOT_APPROVAL_SHA256="<reviewed-approval-sha256>"
CLEAN_START_POLICY="<reviewed-canonical-policy-path>"
CLEAN_START_POLICY_SHA256="<reviewed-policy-sha256>"
MIGRATION_IMAGE_ID="<sha256-image-id>"
MIGRATION_IMAGE_REVISION="<full-40-character-main-sha>"
PREVIOUS_IMAGE_ID="<sha256-previous-image-id>"
EXPECTED_HOSTNAME="<approved-hostname>"
EXPECTED_FREE_BYTES="<approved-minimum-free-bytes>"

SOURCE_DEVICE="$(stat --format='%d' "$DB_PATH")"
SOURCE_INODE="$(stat --format='%i' "$DB_PATH")"
SOURCE_SIZE="$(stat --format='%s' "$DB_PATH")"
SOURCE_SHA256="$(sha256sum "$DB_PATH" | awk '{print $1}')"

sudo "$HOST_PYTHON" "$GATE" plan \
  --repository-root "$REPOSITORY_ROOT" \
  --db-path "$DB_PATH" \
  --backup-parent "$BACKUP_PARENT" \
  --staging-parent "$STAGING_PARENT" \
  --evidence-parent "$EVIDENCE_PARENT" \
  --operations-root-approval "$OPERATIONS_ROOT_APPROVAL" \
  --expected-operations-root-approval-sha256 "$OPERATIONS_ROOT_APPROVAL_SHA256" \
  --clean-start-policy "$CLEAN_START_POLICY" \
  --expected-clean-start-policy-sha256 "$CLEAN_START_POLICY_SHA256" \
  --migration-image-id "$MIGRATION_IMAGE_ID" \
  --migration-image-revision "$MIGRATION_IMAGE_REVISION" \
  --previous-image-id "$PREVIOUS_IMAGE_ID" \
  --expected-hostname "$EXPECTED_HOSTNAME" \
  --expected-source-device "$SOURCE_DEVICE" \
  --expected-source-inode "$SOURCE_INODE" \
  --expected-source-size "$SOURCE_SIZE" \
  --expected-source-sha256 "$SOURCE_SHA256" \
  --expected-free-bytes "$EXPECTED_FREE_BYTES" \
  --expires-in-seconds 3600
```

The reviewer records the values below from the sanitized plan output. They must not
be derived from ambient environment during execute:

```text
APPROVED_PLAN_PATH=<exact-plan-path>
APPROVED_PLAN_SHA256=<exact-plan-sha256>
APPROVED_OPERATION_ID=<exact-operation-id>
APPROVED_SOURCE_SHA256=<exact-source-sha256>
APPROVED_IMAGE_REVISION=<exact-image-revision>
APPROVED_OPERATIONS_ROOT_APPROVAL_SHA256=<exact-approval-sha256>
APPROVED_CLEAN_START_POLICY_SHA256=<exact-policy-sha256>
APPROVED_PLAN_CREATOR_UID=0
APPROVED_PLAN_CREATOR_GID=<recorded-root-group>
APPROVED_TARGET_SCHEMA_VERSION=<derived-version>
APPROVED_TARGET_SCHEMA_FINGERPRINT=<derived-fingerprint>
```

Before execute, verify the plan hash and use the canonical deployment SOT to stop
only hermes-bot. Do not hand-assemble an alternate Compose chain, stop Qdrant, or
mount the production DB into a container. Execute is then limited to:

```bash
set -euo pipefail

test "$(sha256sum "$APPROVED_PLAN_PATH" | awk '{print $1}')" = "$APPROVED_PLAN_SHA256"

sudo "$HOST_PYTHON" "$GATE" execute \
  --plan "$APPROVED_PLAN_PATH" \
  --expected-plan-sha256 "$APPROVED_PLAN_SHA256" \
  --confirm-operation-id "$APPROVED_OPERATION_ID" \
  --confirm-source-sha256 "$APPROVED_SOURCE_SHA256" \
  --confirm-image-revision "$APPROVED_IMAGE_REVISION" \
  --confirm-operations-root-approval-sha256 "$APPROVED_OPERATIONS_ROOT_APPROVAL_SHA256" \
  --confirm-clean-start-policy-sha256 "$APPROVED_CLEAN_START_POLICY_SHA256"
```

If execute cannot acquire the source SQLite lease, it must stop before creating
`execution.json`, backup, staging, or internal-manifest artifacts. Do not treat
the immutable plan from the separate plan command as execute-time filesystem
mutation.

A pre-publish failure leaves the original target live. A proved reverse exchange
restores the original inode and fsyncs the parent. Any post-exchange uncertainty is
PUBLISH_UNCERTAIN followed by MANUAL_RECOVERY_REQUIRED; automatic retry and blind
DB restore are forbidden. DB restoration is a separate explicit recovery procedure.
Image rollback is allowed only after the previous-image compatibility probe
succeeds against the migrated schema.

Successful public execution evidence has this monotonic state stream:

```text
QUIESCENCE_HELD -> COMPLETED
```

The separately pinned internal staged manifest has this monotonic state stream:

```text
PLANNED -> BACKED_UP -> MIGRATED -> VALIDATED -> PUBLISHED -> VERIFIED
```

Its publish-state stream is `BEFORE_EXCHANGE`, `EXCHANGE_STARTED`,
`EXCHANGE_COMPLETED_NOT_VERIFIED`, `EXCHANGE_VERIFIED_NOT_FSYNCED`,
`PARENT_FSYNCED`, and `FINAL_VERIFIED`. The plan path, deployment contract,
operation parents, and internal manifest directory descriptors remain pinned
through completion and are never reopened as unverified authority.

It must also record backup/staging/final SHA-256 values without DB contents,
identifiers, credentials, environment dumps, or raw logs. Evidence directory mode
is 0700; plan and execution files are 0600. The durable backup and evidence are
retained after success or failure.

After a successful execute, perform read-only DB verification, then use only the
canonical deployment entrypoint to start the exact image with feature gates still
disabled. The command below remains an example requiring separate deploy approval:

```bash
set -euo pipefail

sudo "$HOST_PYTHON" scripts/hermes_production_deploy.py execute-deploy \
  --image "$MIGRATION_IMAGE_ID" \
  --revision "$MIGRATION_IMAGE_REVISION" \
  --confirm DEPLOY_HERMES_BOT
```

No inline Python, in-container exec-based migration, direct production DB bind
mount, automatic
feature enablement, automatic DB restore, or automatic retry is permitted.

The one-shot lock probe is only a preflight signal. Before backup creation, the
orchestrator pins the source file and its parent directory descriptors and acquires
an exclusive SQLite-compatible source lease on that exact inode. The lease remains
held continuously through copy, migration, publish, parent fsync, and final
verification. After validation and previous-image startup, the orchestrator acquires a
second exclusive SQLite-compatible lease on the pinned staging inode and holds it
through final verification. Poll-only quiescence and unrelated generic `flock`
locks are not sufficient. Separate SQLite readers and writers must be refused at
every lifecycle boundary. These controls do not claim protection from host root or
another privileged administrator.

After quiescence, the root orchestrator must create and fsync a durable
backup, then create a byte-identical staging copy on the same filesystem as the
target. Both files must derive from the same quiesced source identity and hash.
The staging parent is root-owned and exactly mode `0700`; only its operation-owned
child directory is owned by `10000:10000` and mode `0700`. The staging DB is mode
`0600`, has link count one, and has no symlink component.

Path-security preconditions are part of the migration authorization, not a post-run
check:

```text
only the disposable staging directory is mounted read/write at /migration
the production DB path and production parent are not mounted
the container runs by exact image ID with network none and UID/GID 10000:10000
no production secrets are mounted
the CLI receives /migration/database.sqlite with explicit --staged-copy
--staged-copy and --synthetic-create are mutually exclusive
the CLI reports PATH_MODE=STAGED_COPY
the CLI performs no host publish operation
normal SQLite DELETE journaling and synchronous FULL remain enabled
journal_mode OFF, journal_mode MEMORY, and disabled synchronous are prohibited
```

The writable directory is safe only because it contains a disposable copy and is
private to one isolated one-shot process. A failed or substituted staging output
cannot affect production and must be rejected before publish. Host root remains the
only publish authority.

Required command properties:

```text
explicit --db-path is mandatory
immutable image ID is used
network is disabled
runtime UID/GID is explicit
no production secrets are mounted
no main Hermes service starts
no Qdrant, Telegram, provider, scheduler, or background worker starts
no feature flag is changed
no snapshot or shopping backfill is performed
no command is executed inside the running production service for migration
staged-copy mode is required; synthetic-create and protected-existing modes are forbidden
```

Expected migration sequence:

```text
household schema initialization
weekly-menu schema initialization
shopping schema initialization
```

The public CLI performs the weekly phase before the shopping phase in one deterministic command. The heading below is retained as a compatibility marker for the production readiness contract; do not replace it with inline Python.

### D3 Shopping schema initialization

The shopping phase is executed by the same public CLI invocation after weekly schema reaches canonical state. Do not use in-service execution or inline Python for this phase.


Required successful sanitized JSON output:

```text
status = success
exit_classification = SUCCESS
migration_commit_state = COMMITTED
schema_may_have_changed = true
cleanup_failed = false
safe_to_rerun = true
HOUSEHOLD schema_state = CURRENT
WEEKLY schema_state = CURRENT
SHOPPING schema_state = CURRENT
data_backfilled = false
path_mode = STAGED_COPY
```

Public stable exit classifications and deterministic precedence (highest priority first after `SUCCESS`):

```text
0  SUCCESS
2  INVALID_ARGUMENT
3  UNSAFE_PATH
4  MISSING_DATABASE
8  DATABASE_PERMISSION_DENIED
7  DATABASE_READ_ONLY
6  DATABASE_LOCKED
5  INCOMPATIBLE_SCHEMA
9  MIGRATION_FAILED
10 CLEANUP_FAILED
11 CONTRACT_DRIFT
```

This order is the classification source of truth when an operation exposes more
than one failure. Cleanup state is reported separately and never masks the primary
operational classification. SQLite `sqlite_errorcode`, `sqlite_errorname`, and
known result codes are used when structured metadata is present. Exception-message
text is never used to infer a specific operational class. When structured metadata
is absent or insufficient, the CLI reports `MIGRATION_FAILED`; operators must not
infer locked, read-only, or permission status from exception text.

The migration command requires an existing regular staging database file. No
create-mode flag is permitted. Synthetic mode is limited to temporary local tests
and must never appear in a production execution manifest.

Any nonzero classification is terminal for this attempt. Do not retry automatically,
do not repair DDL manually, and do not continue to service recreation.

Required post-conditions:

```text
household schema state = canonical
weekly schema state = canonical
shopping schema state = canonical
weekly series/revision/entry counts unchanged from baseline
series_count = 0 before first weekly feature use unless already present before migration
revision_count = 0 before first weekly feature use unless already present before migration
entry_count = 0 before first weekly feature use unless already present before migration
shopping list/item/idempotency counts = 0 unless already present before migration
list_count = 0 before first shopping feature use unless already present before migration
item_count = 0 before first shopping feature use unless already present before migration
idempotency_count = 0 before first shopping feature use unless already present before migration
nutrition/profile/weight/water/reminder counts unchanged
DB owner and mode unchanged
staging journal, WAL, and SHM sidecars absent after close
staging integrity_check = ok and foreign_key_check = 0
expected schema complete and unknown schema objects = 0
all pre-existing table counts unchanged
new weekly snapshot and shopping business rows = 0
three staged migration runs have zero schema and data delta after the first run
previous production image reaches persisted gateway_state=running through its
canonical /init entrypoint with both features disabled, network none, no production
secrets, no automatic migration, no DB mutation, and a clean shutdown
```

### D3 atomic publish boundary

Only after all staging validations and previous-image compatibility pass may future
production execution publish. Source DB, staging DB, target parent, and staging
parent descriptors remain pinned. The Linux publish primitive is same-filesystem
`renameat2(..., RENAME_EXCHANGE)` through the pinned directory descriptors;
cross-filesystem publication fails closed, as does any platform without
`RENAME_EXCHANGE`.
There is no `os.replace` fallback. The migrated DB is fsynced before exchange and
both affected parent directories are fsynced after exchange. Bytes are never copied
over the live target and incomplete validation can never publish.

Immediately after exchange, the displaced staging pathname must resolve to the
previously pinned source inode and the live target pathname must resolve to the
pinned migrated inode. An identity mismatch triggers one reverse exchange while
both SQLite leases are still held, followed by parent fsync and `CONTRACT_DRIFT`.
Automatic retry remains prohibited. If reversal cannot be proved, publish state is
uncertain and manual recovery is mandatory.

A durable sanitized manifest records only operation metadata and monotonic states:

```text
PLANNED -> BACKED_UP -> MIGRATED -> VALIDATED -> PUBLISHED -> VERIFIED
FAILED is terminal for a known pre-publish failure
unknown state fails closed
PUBLISH_STATE=BEFORE_EXCHANGE before any exchange attempt
PUBLISH_STATE=EXCHANGE_STARTED is durably recorded before the syscall
PUBLISH_STATE=EXCHANGE_COMPLETED_NOT_VERIFIED after the syscall returns
PUBLISH_STATE=EXCHANGE_VERIFIED_NOT_FSYNCED after inode verification
PUBLISH_STATE=PARENT_FSYNCED after durable parent fsync
PUBLISH_STATE=FINAL_VERIFIED only after pinned-inode SQLite verification
Legacy PUBLISH_STATE=UNKNOWN is never emitted; any unrecognized state fails closed.
automatic retry is prohibited
manual recovery is required for every failure after EXCHANGE_STARTED
```

Every exception after `EXCHANGE_STARTED`, including internal manifest I/O, final
target validation or hashing, plan-path revalidation, external evidence I/O,
completion transition, and operation cleanup, is classified centrally as
`PUBLISH_UNCERTAIN`. If durable evidence persistence itself fails, the gate emits
one sanitized machine-readable stderr result, does not claim persistence, forbids
automatic retry, and requires manual inspection. Only a verified reverse exchange
followed by parent fsync may report the original target restored.

The manifest contains source/staging/backup paths, inode and SHA-256 values, but no
DB contents, application identifiers, credentials, or user data. Every manifest
transition and containing directory is fsynced.

Failure before `EXCHANGE_STARTED` removes only the operation-owned staging tree and
leaves the original target unchanged. Cleanup revalidates the saved staging-root and
operation-directory device/inode, private ownership and modes, rejects symlink or
nested-directory traversal, and refuses to unlink the live target or displaced
source inode. Backup, manifest, and sanitized evidence are retained. A cleanup
failure is reported separately and never masks the primary error. Machine-readable
results preserve the last valid primary classification and publish state in
`primary_exit_classification`, `primary_publish_state`,
`primary_target_may_have_changed`, `primary_automatic_retry_allowed`,
`primary_manual_recovery_required`, and `primary_exception_present`. Cleanup is
reported independently through `cleanup_exception_count` and `cleanup_failures`;
each cleanup record is restricted to `resource_kind`, `cleanup_phase`,
`error_type`, and `error_code`. Exception messages, paths, identifiers, and
credentials are never included. The durable evidence uses the corresponding
uppercase field names. If evidence cannot be updated, the sanitized stderr result
sets `durable_evidence_updated=false` and retains the same primary/cleanup
separation.

After `EXCHANGE_STARTED`, or whenever publish state is uncertain, automatic staging deletion is forbidden because the staging pathname may contain the displaced source
DB required for recovery. The backup and manifest remain durable and no automatic
retry is allowed.

Image rollback after a successful additive migration uses the migrated DB and does
not restore the pre-migration backup. Backup restore is an emergency manual action only.
It is forbidden after new application writes unless the operator explicitly
accepts the resulting data-loss window.

Failure rules:

```text
nonzero migration exit blocks deployment
no automatic retry
no manual DDL repair
retain the fresh backup
open a separate recovery task
```

## D4 - Disabled-State Observation and Rollback Verification

After D3 completes:

```text
features remain disabled
allowlists remain empty
Telegram remains non-mutating
no provider calls
no background generation
no unexpected DB writes
Hermes remains healthy
Qdrant remains unchanged
privacy-safe logs only
```

Expected Telegram smoke while flags stay disabled:

```text
/start works
main menu works
existing non-HealBite functions work
📋 Меню на неделю -> В разработке
🛒 Список покупок -> В разработке
👨‍👩‍👧 Семья -> В разработке
```

## D5 - Later Allowlist Canary

D5 is a separate future approval.

Do not combine D5 with D1-D4.

```text
no automatic canary enablement
no real allowlist values in D0, D1, D2, D3, or D4
no Telegram mutation UI enablement in D0-D4
```

## Provider Isolation Contract

Feature-disabled rollout must prove:

```text
weekly generator provider calls = 0
no provider client required for startup
no scheduled generation
no background generation
no provider secrets needed for disabled startup
```

## Rollback Taxonomy

### Image-only rollback

Use when:

```text
new image unhealthy
schema DDL not executed yet
```

Steps:

```text
restore previous exact image ID
recreate hermes-bot only
verify Qdrant unchanged
verify DB unchanged
verify Telegram baseline restored
```

### Post-schema image rollback

Use when additive schema was initialized but the new image must be removed:

```text
restore previous image only
do not drop weekly or shopping tables automatically
keep features disabled
prove old image tolerates additive unused tables
verify health and Telegram baseline
```

### DB rollback

Allowed only for confirmed corruption:

```text
stop Hermes safely
preserve failed DB copy
restore verified backup atomically
verify integrity_check and foreign_key_check
restart approved image only after DB proof
```

Destructive `DROP TABLE` is prohibited as ordinary rollback.

## Old-Image Additive-Schema Compatibility

The currently deployed source revision `04566a0dd2b79f60748194cc3d318c5a5e75f3d3` contains no weekly-menu or shopping runtime surface in `gateway/run.py`, `docker-compose.yml`, or the startup path.

Read-only source audit result:

```text
old image does not reference weekly-menu startup modules
old image does not reference shopping startup modules
old image does not run weekly/shopping destructive schema checks
old image startup contract is independent from unknown additive weekly/shopping tables
```

Therefore additive unused weekly/shopping tables are expected to be rollback-compatible with the old image, provided:

```text
features stay disabled
allowlists stay empty
no weekly/shopping startup hooks are introduced outside approved image
```

If a later source audit contradicts that assumption, stop before D3 and resolve compatibility first.

## Stop / Go Criteria

### STOP before deploy

```text
main SHA mismatch
dirty build context
tests failed
unknown production DB path
unknown feature configuration
Qdrant identity unavailable
insufficient disk space
backup destination invalid
production baseline mismatch
```

### STOP after image deploy

```text
Hermes unhealthy
restart_count != 0
unexpected DB writes
schema auto-created
feature unexpectedly enabled
allowlist unexpectedly nonempty
Qdrant changed
existing bot regression
```

### STOP after schema init

```text
weekly schema not canonical
shopping schema not canonical
business rows created
households count changed
household_members count changed
existing data counts changed unexpectedly
integrity_check failed
foreign_key_check nonempty
Telegram behavior changed
```

## Evidence Template

Use only safe evidence:

```text
Approved Source SHA
Built Image Tag
Built Image ID
Inside-Image Revision
Old Hermes Container ID
New Hermes Container ID
Old Hermes Image ID
New Hermes Image ID
Qdrant Container ID Before
Qdrant Container ID After
Qdrant Image ID Before
Qdrant Image ID After
Production DB Path
Backup Path
Backup SHA-256
Backup Integrity
Preflight DB Integrity
Preflight FK Check
Households Before/After
Members Before/After
Weekly Schema Before/After
Shopping Schema Before/After
Weekly Business Rows
Shopping Business Rows
Existing Data Count Deltas
Weekly Feature Enabled
Shopping Feature Enabled
Weekly Allowlist Count
Shopping Allowlist Count
Startup DDL Detected
Provider Calls Detected
Telegram Smoke
Rollback Required
Final Verdict
```

Do not print:

```text
Telegram IDs
application user IDs
household IDs
member IDs
allowlist contents
menu rows
shopping rows
profile values
nutrition targets
allergies
provider responses
full environment
API keys or secrets
```

## Safety Rules for Future Commands

All future shell blocks must:

```text
use set -euo pipefail
check expected SHA
check exact service/container identity
avoid docker system prune
avoid docker compose down
avoid volume removal
avoid git reset --hard in canonical checkout
avoid git clean
avoid force push
avoid raw secret output
avoid real user/member/household IDs
mark every state-changing command as D1-D4 only
```

## Final D0 Verdict Contract

This runbook is complete only if a reviewer can prove:

```text
exact SHA build contract documented
Hermes-only recreate documented
Qdrant isolation documented
features disabled by default
allowlists empty by default
no startup schema auto-init
backup before first DDL
weekly schema initialized before shopping
zero business rows after init
image rollback and DB rollback separated
no feature enablement in this rollout
production IDs prohibited
```
