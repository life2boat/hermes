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
The exact 40-character source revision is also mandatory.

## Repository validation

Run from any directory; the wrapper resolves its own repository root:

```bash
scripts/hermes_production_deploy.sh check-repository \
  --expected-sha <exact-40-character-source-sha>
```

This mode checks HEAD, clean worktree state, canonical files, project/service
identity, disabled Shopping flags, and absence of active legacy paths. It does
not invoke Docker or read secret values.

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

The preferred plan command prepares and cleans the override automatically,
including after failures:

```bash
scripts/hermes_production_deploy.sh plan \
  --secret-source /etc/hermes/hermes-production.env \
  --image sha256:<64-hex-image-id> \
  --revision <exact-40-character-source-sha>
```

Planning validates local image availability and Compose rendering but never
builds, pulls, starts, stops, or recreates a container.

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

The wrapper renders the same canonical chain, recreates only `hermes-bot` with
`--no-deps --force-recreate`, verifies running state, restart count zero and
image identity, then removes the runtime override. Qdrant is not in the recreate
plan. Do not invoke this mode without a dedicated controlled-deploy task.

## Cleanup lifecycle

```bash
scripts/hermes_production_deploy.sh cleanup
```

Cleanup is idempotent and can remove only the exact canonical override. It
rejects symlinks and unexpected paths and never touches the source dotenv file.
Run it after a failed manual prepare/render, after deployment, and after rollback.
The `/run` filesystem also clears at reboot. Automated plan, deploy, and rollback
modes clean the override in `finally` behavior.

## Application rollback

Prepare and validate an image-only rollback without changing production:

```bash
scripts/hermes_production_deploy.sh plan-rollback \
  --secret-source /etc/hermes/hermes-production.env \
  --image sha256:<64-hex-previous-image-id> \
  --current-image sha256:<64-hex-current-image-id> \
  --revision <exact-40-character-previous-source-sha>
```

The previous and current image IDs must both exist locally and differ. The plan
uses the canonical project, Compose files, protected override, and target service.
The additive Weekly/Shopping schema remains in place; schema downgrade and DB
restore are not part of application rollback.

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
