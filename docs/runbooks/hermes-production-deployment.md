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

The Python package declared by the exact `google-meet` optional dependency in
`pyproject.toml` and resolved with hashes in `uv.lock` is the single
authoritative Playwright runtime. Node Playwright is not a browser-install
authority. The browser family, revision, platform mapping, cache directory, and
executable path must be derived from that installed package's bundled
`browsers.json` metadata:

```bash
.venv/bin/python scripts/playwright_artifact_contract.py \
  --platform linux/amd64
```

This command is read-only. It neither installs a browser nor makes a network
request. A browser archive must be acquired and approved in a separate task.
Its immutable manifest SHA-256 must be known and reviewed before a canonical
build starts; computing a digest after an unreviewed download is not approval.
Do not put an archive, an instantiated manifest, a credential, or a production
secret in Git, the normal repository build context, or build arguments.

The approved artifact directory must be an absolute path outside the
repository, must not contain symlinks or group/world-writable paths, and must
contain exactly these fixed names:

```text
manifest.json
browser-archive
```

`manifest.json` must use the canonical schema in
`schemas/playwright-artifact-manifest.schema.json`. Its package, revision,
platform, cache root, archive size, archive SHA-256, and executable path are
mandatory. The source reference is an opaque approval reference, not a URL.
The build has no Playwright CDN fallback.

Validate exact source and approved inputs without invoking Docker:

```bash
EXACT_SHA=<exact-40-character-source-sha>
ARTIFACT_DIR=<absolute-approved-directory-outside-repository>
MANIFEST_SHA256=<predeclared-reviewed-lowercase-sha256>
IMAGE_REF="healbite-hermes:playwright-${EXACT_SHA:0:12}"

.venv/bin/python scripts/build_verified_playwright_image.py check \
  --expected-source-sha "$EXACT_SHA" \
  --artifact-context "$ARTIFACT_DIR" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  --image-tag "$IMAGE_REF" \
  --platform linux/amd64
```

Only a separately authorized image-build task may replace `check` with
`build`. That mode uses one read-only BuildKit named context and embeds the
exact OCI revision:

```text
playwright_artifact=<approved artifact directory>
org.opencontainers.image.revision=<exact source SHA>
```

The canonical helper rejects a dirty or mismatched source tree, a mutable or
unrelated image tag, a missing artifact context, a missing or incorrect
manifest digest, additional context files, writable/symlinked inputs, and an
archive whose size or digest differs from the manifest. The Docker installer
then re-derives browser identity from the pinned package metadata, validates
the canonical manifest and archive before extraction, rejects unsafe archive
entries, and publishes the cache directory atomically. It never falls back to
an external browser download.

Direct `docker build`, `docker compose build`, an implicitly resolving `npx`,
and a normal Playwright browser-install command are non-authoritative for a
production image. Artifact acquisition, image build, image validation, and
deployment remain separate approval gates.

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
