# Hermes production deployment source of truth

This runbook and `scripts/hermes_production_deploy.sh` are the only authoritative
application deployment entrypoint for Hermes production. Database migration,
production snapshot inventory, snapshot backfill, and feature enablement are
separate approval gates.

## Canonical contract

| Setting | Value |
| --- | --- |
| Manifest | `deploy/hermes-production.json` |
| Base Compose file | `docker-compose.yml` |
| Production override | `deploy/docker-compose.production.yml` |
| Runtime secret override | `/run/hermes/hermes-secrets-override.yml` |
| Project directory | repository root resolved from the script location |
| Compose project | `hermes-agent` |
| Target service | `hermes-bot` |
| Runtime directory | `/run/hermes`, deployment-operator-owned, mode `0700` |
| Secret override | regular deployment-operator-owned file, mode `0600` |
| Approved secret source class | explicit protected dotenv file outside the repository |
| Approved production source | `/etc/hermes/hermes-production.env`, root-owned, mode `0600` |
| Required override variable | `TELEGRAM_BOT_TOKEN` |

The canonical Compose order is deterministic:

1. `docker-compose.yml`
2. `deploy/docker-compose.production.yml`
3. `/run/hermes/hermes-secrets-override.yml`

The production override explicitly keeps `HEALBITE_SHOPPING_LIST_ENABLED=false`
and its allowlist empty. Other feature settings are not changed by this contract.

Image inputs must be immutable Docker image IDs (`sha256:<64 hex>`) or repository
digests (`name@sha256:<64 hex>`). Mutable tags, including `latest`, are rejected.
The exact 40-character source revision is also mandatory. Every deployable or
rollback image must contain that same full SHA in the single authoritative OCI
label `org.opencontainers.image.revision`. A missing, malformed, abbreviated, or
mismatched label is a hard failure; tag text is never treated as provenance.

## Verified Playwright image build prerequisite

The exact `playwright` package entry in `uv.lock` is the package authority for
the complete runtime closure. The canonical contract selects one platform
wheel, verifies its filename, size, and SHA-256 against the lock entry, and
reads bundled `browsers.json` directly from those verified wheel bytes. The
required artifact set, revisions, optional browser versions, platform
overrides, and cache directories are derived from that metadata. Installed
package metadata is used only for packaged readiness and cannot replace the
verified wheel as build authority.

For the current Chromium `--only-shell` runtime, the verified closure contains
exactly `chromium-headless-shell` and `ffmpeg`. FFmpeg is required, never
optional. Chromium uses the strict `DIRECTORY_TREE` layout; FFmpeg uses the
strict `SINGLE_EXECUTABLE_FILE` layout. No generic permissive archive layout or
legacy single-artifact manifest is accepted.

The approved closure directory must be an absolute path outside the repository,
must not contain symlinks or group/world-writable paths, and has this exact
shape:

```text
closure.json
playwright-wheel
artifacts/
  chromium-headless-shell/
    archive
  ffmpeg/
    archive
```

`closure.json` uses the canonical schema in
`schemas/playwright-artifact-manifest.schema.json`. It is canonical JSON with
strict field types and no unknown fields. It binds the lock-authorized wheel,
platform, exact ordered artifact set, artifact-specific revisions and layouts,
archive sizes and SHA-256 values, executable paths, and opaque approval-source
digests. Missing, duplicate, unreferenced, or unexpected artifacts and files
are denied. The SHA-256 of this aggregate closure manifest is the single
mandatory artifact build input; a child artifact cannot be accepted outside
that binding.

Acquisition and approval of every archive happen in a separate task. Immutable
hashes must be known and reviewed before a canonical build starts. Computing a
digest after an unreviewed acquisition is not approval. Do not put an archive,
instantiated closure, credential, browser profile, user data, production
secret, patch, or evidence in Git or the ordinary Docker build context. This
contract contains no artifact URL or download command and has no Playwright CDN
fallback.

Report the lock-bound closure without installing artifacts or making a network
request:

```bash
ARTIFACT_DIR=<absolute-approved-directory-outside-repository>

.venv/bin/python scripts/playwright_artifact_contract.py \
  --lockfile uv.lock \
  --wheel "$ARTIFACT_DIR/playwright-wheel" \
  --platform linux/amd64
```

Validate exact source and the complete approved closure without invoking
Docker:

```bash
EXACT_SHA=<exact-40-character-source-sha>
APPROVED_BASE_SHA=<exact-40-character-approved-base-sha>
ARTIFACT_DIR=<absolute-approved-directory-outside-repository>
CLOSURE_MANIFEST_SHA256=<predeclared-reviewed-lowercase-sha256>
IMAGE_REF="healbite-hermes:playwright-${EXACT_SHA:0:12}"

.venv/bin/python scripts/build_verified_playwright_image.py check \
  --expected-source-sha "$EXACT_SHA" \
  --approved-base-sha "$APPROVED_BASE_SHA" \
  --artifact-context "$ARTIFACT_DIR" \
  --expected-closure-manifest-sha256 "$CLOSURE_MANIFEST_SHA256" \
  --image-tag "$IMAGE_REF" \
  --platform linux/amd64
```

Only a separately authorized image-build task may replace `check` with
`build`. Both modes export the exact requested Git tree into an operation-owned
temporary directory, verify every path, mode, and blob identity, create and
re-read a context manifest, and reject submodules, Git LFS pointers, secrets,
databases, patch files, caches, evidence, and local review mirrors. Ignored,
untracked, and other raw-worktree content never enters the Docker context.

The approved base SHA is mandatory, must resolve to an ancestor of the exact
source SHA, and is recorded with its tree identity in the context manifest.
Regular-file blob OIDs already present in that immutable approved base are
provenance-bound. Every candidate object not present in the approved base is
read from the commit tree with Git plumbing and passed through the same
Git-object policy that reads staged candidates from the index. Worktree bytes
are never the authority for either caller.

Secret classification is deterministic and shared by the repository and
exported-context checks. Regular candidate blobs containing NUL bytes, invalid
UTF-8, or content beyond the complete-scan limit are denied. Symlinks,
gitlinks, unknown modes, missing objects, read failures, and internal scanner
failures are also denied without following a worktree target or traversing a
submodule. Credential variable names without assigned values, documented
placeholders, redaction-pattern definitions, and marker-only test fixtures are
not secret material. Complete private-key blocks, credential-bearing URLs with
secret-shaped values, provider-token-shaped assignments, and high-entropy
credential assignments are denied regardless of path. No filename or directory
allowlist can bypass content classification, and scanner failures remain fail
closed.

The Docker build receives the exported Git tree as its ordinary context and one
read-only BuildKit named context:

```text
playwright_artifacts=<approved complete closure directory>
org.opencontainers.image.revision=<exact source SHA>
```

Before extraction, the installer verifies the closure manifest digest,
canonical manifest, exact wheel-derived set, every archive size/hash, and each
artifact-specific layout. It then extracts all artifacts into one same-filesystem
staging cache, verifies both executables, fsyncs regular files and directories
bottom-up, and atomically publishes the complete cache root with one rename.
The final parent is fsynced and both published artifacts are reopened and
revalidated. No artifact is published before the full closure validates.

The immutable installed marker binds the aggregate closure-manifest digest,
both archive digests, and deterministic full-tree digests for Chromium and
FFmpeg. A separate root-owned expected-closure identity beside the cache is the
build-time trust anchor; it is never sourced from an environment variable or
from the marker itself. The cache, marker, and expected identity are sealed
against runtime writes. Post-publication validation and runtime readiness hash
every installed regular file and bind directory structure and modes. Browser
profiles, user data, and temporary launch state remain in runtime-owned paths
outside `/opt/hermes/.playwright`; the immutable cache is never made writable
to launch Chromium.

A complete matching existing cache is accepted after full revalidation. An
incomplete cache, mixed revision, package mismatch, missing executable, altered
file, extra file, permission change, or unexpected cache entry is denied and is
never merged with staged content. A Chromium-only or FFmpeg-only cache is not
Ready. Packaged Google Meet readiness requires both exact artifacts and performs
no download attempt.

Artifact acquisition, image build, image validation, deployment, and feature
activation remain separate approval gates.

## Repository validation

Run from any directory; the wrapper resolves its own repository root:

```bash
scripts/hermes_production_deploy.sh check-repository \
  --expected-sha <exact-40-character-source-sha>
```

This mode checks the canonical repository root, exact HEAD, reachability from
`refs/remotes/healbite-project/main`, clean worktree state, canonical files,
project/service identity, disabled Shopping flags, and absence of active legacy
paths. It does not invoke Docker or read secret values.

## Protected secret source and producer

The versioned manifest fixes the default secret source at
`/etc/hermes/hermes-production.env`. An explicit `--secret-source` file argument
is supported but must resolve to that approved path. Ambient
`TELEGRAM_BOT_TOKEN`, repository dotenv files, backup files, and the legacy
runtime override are never accepted as substitutes.

Metadata and required-name validation:

```bash
scripts/hermes_production_deploy.sh check-secret-source \
  --secret-source /etc/hermes/hermes-production.env
```

Create the protected override atomically:

```bash
scripts/hermes_production_deploy.sh prepare-override \
  --secret-source /etc/hermes/hermes-production.env
```

The producer rejects symlinked path components, insecure source metadata,
missing or duplicate variables, and unexpected source paths. It creates a
same-directory temporary file under a restrictive umask, flushes it, replaces
the deterministic output atomically, fsyncs the directory, and removes partial
temporary files after failures. Status output contains variable names only.

Never use `set -x`, print the source, render full Compose configuration, pass
secret values on the command line, or store rendered configuration as evidence.

## Check-only render and plan

`check-render` requires an already prepared override and uses `docker compose
config --quiet` plus a service-name-only query. It suppresses Compose output:

```bash
scripts/hermes_production_deploy.sh check-render \
  --image sha256:<64-hex-image-id> \
  --revision <exact-40-character-source-sha>
scripts/hermes_production_deploy.sh cleanup
```

The preferred plan command creates its override only inside a unique private
temporary directory and removes both the override and directory automatically,
including after failures:

```bash
scripts/hermes_production_deploy.sh plan \
  --secret-source /etc/hermes/hermes-production.env \
  --image sha256:<64-hex-image-id> \
  --revision <exact-40-character-source-sha>
```

Planning validates repository provenance, local immutable image availability,
the OCI revision label, exact image/revision equality, the protected secret
source, and Compose rendering. It never creates `/run/hermes`, touches the
legacy `/tmp` override, retains rendered configuration, or builds, pulls,
starts, stops, or recreates a container. A successful plan is not authorization
to deploy.

## Controlled deployment

Deployment is a separate explicitly confirmed mode. A deployment task must first
prove backups, DB prerequisites, capacity, rollback image, and authorization:

```bash
scripts/hermes_production_deploy.sh execute-deploy \
  --secret-source /etc/hermes/hermes-production.env \
  --image sha256:<64-hex-image-id> \
  --revision <exact-40-character-source-sha> \
  --confirm DEPLOY_HERMES_BOT
```

The execute path independently reruns every repository, revision, image-label,
secret-source, and Compose gate. These checks use an ephemeral override; only
after all pass may execution create `/run/hermes`. Deployment uses the exact
inspected immutable image ID rather than returning to the supplied reference.
The wrapper recreates only `hermes-bot` with `--no-deps --force-recreate`,
verifies running state, restart count zero and image identity, then removes the
runtime override. Qdrant is not in the recreate plan. Do not invoke this mode
without a dedicated controlled-deploy task.

## Cleanup lifecycle

```bash
scripts/hermes_production_deploy.sh cleanup
```

Cleanup is idempotent and can remove only the exact canonical override. It
rejects symlinks and unexpected paths and never touches the source dotenv file.
Run it after a failed manual prepare/render, after deployment, and after rollback.
The `/run` filesystem also clears at reboot. Execute deploy and rollback modes
clean the canonical override in `finally` behavior. Plan modes never use this
path and clean only their private temporary override.

## Application rollback

Prepare and validate an image-only rollback without changing production:

```bash
scripts/hermes_production_deploy.sh plan-rollback \
  --secret-source /etc/hermes/hermes-production.env \
  --image sha256:<64-hex-previous-image-id> \
  --current-image sha256:<64-hex-current-image-id> \
  --revision <exact-40-character-previous-source-sha>
```

The previous and current immutable images must both exist locally and differ.
The previous image's authoritative OCI revision label must exactly equal the
requested rollback SHA. The plan uses the canonical project and Compose files
with an ephemeral protected override; it never creates `/run/hermes`. The
additive Weekly/Shopping schema remains in place; schema downgrade and DB restore
are not part of application rollback.

An approved rollback task may execute:

```bash
scripts/hermes_production_deploy.sh execute-rollback \
  --secret-source /etc/hermes/hermes-production.env \
  --image sha256:<64-hex-previous-image-id> \
  --current-image sha256:<64-hex-current-image-id> \
  --revision <exact-40-character-previous-source-sha> \
  --confirm ROLLBACK_HERMES_BOT
```

Perform the same post-operation health checks and keep Shopping disabled.

## Non-authoritative legacy chain

The existing legacy deployment remains active until a separately authorized
controlled rollout proves the canonical chain. These historical resources are
non-authoritative for the new tooling and must never be selected as secret
sources:

- `/home/hermes/.hermes/worktrees/healbite-s71v2-r6-deploy-22ed9e4`
- `/tmp/hermes-secrets-override.yml`

Do not delete or modify those host resources before a successful controlled
rollout. The canonical wrapper has no dependency on either path. Migration,
snapshot inventory, and snapshot backfill remain separately approved tasks.
