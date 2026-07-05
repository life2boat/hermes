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

docker exec "$EXACT_CONTAINER" cat "$BUILD_SHA_FILE"
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

docker exec "$EXACT_CONTAINER" "$IMAGE_PYTHON" - <<'PY'
import sqlite3
src = sqlite3.connect("/home/hermes/healbite.db")
dst = sqlite3.connect("/home/hermes/backups/PLACEHOLDER/healbite.db")
with dst:
    src.backup(dst)
dst.close()
src.close()
PY

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

Use the canonical store initializer inside the exact deployed image namespace:

```bash
set -euo pipefail

docker exec "$EXACT_CONTAINER" "$IMAGE_PYTHON" - <<'PY'
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
store = HealBiteWeeklyMenuStore(db_path="/home/hermes/healbite.db")
state = store.initialize_schema()
print(f"weekly_schema_state={state.value}")
audit = store.audit_schema()
print(f"weekly_series_count={audit.series_count}")
print(f"weekly_revision_count={audit.revision_count}")
print(f"weekly_entry_count={audit.entry_count}")
print(f"weekly_idempotency_count=0")
PY
```

Required post-conditions:

```text
weekly schema state = canonical
partial schema must be refused
incompatible schema must be refused
series_count = 0
revision_count = 0
entry_count = 0
households unchanged
household_members unchanged
nutrition/profile/weight/water/reminder counts unchanged
```

### D3 Shopping schema initialization

Only after weekly schema is canonical:

```bash
set -euo pipefail

docker exec "$EXACT_CONTAINER" "$IMAGE_PYTHON" - <<'PY'
from gateway.healbite_shopping import HealBiteShoppingStore
store = HealBiteShoppingStore(db_path="/home/hermes/healbite.db")
state = store.initialize_schema()
print(f"shopping_schema_state={state.value}")
audit = store.audit_schema()
print(f"shopping_list_count={audit.list_count}")
print(f"shopping_item_count={audit.item_count}")
print(f"shopping_idempotency_count={audit.idempotency_count}")
PY
```

Required post-conditions:

```text
shopping schema state = canonical
dependency_missing must be refused before weekly canonical
partial schema must be refused
incompatible schema must be refused
list_count = 0
item_count = 0
idempotency_count = 0
weekly schema remains canonical
existing household/member/health data counts unchanged
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