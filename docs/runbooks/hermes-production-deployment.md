# Hermes Production Deployment Source of Truth

Status: **PARTIAL / deployment blocked** until the secrets-override producer and lifecycle are separately authorized and proven.

This runbook defines the source inputs and read-only gate for Hermes/HealBite deployment. It does not grant production authority and contains no credentials, fingerprints, user identifiers or private network addresses.

## Immutable source contract

Every deployment starts from a clean detached or feature worktree at the exact approved project SHA. The dirty canonical checkout is never a deployment source.

```bash
git fetch healbite-project --prune
git rev-parse healbite-project/main
git -C "$DEPLOY_WORKTREE" rev-parse HEAD
git -C "$DEPLOY_WORKTREE" status --porcelain=v1
```

The approved SHA, remote main SHA and worktree HEAD must be identical. Any output from `git status --porcelain=v1` is a hard stop.

The tracked contract is:

```text
deploy/hermes-production.manifest.yml
```

Historical worktree paths shown by Docker labels are evidence of the last rollout, not permanent repository inputs.

## Provenance classification

| Input | Status | Evidence | Runtime dependency | Reproducible |
|---|---|---|---|---|
| Compose project `hermes-agent` | PROVEN | Docker Compose labels and corrected rollout evidence | Compose CLI | Yes |
| Application service `hermes-bot` | PROVEN | Compose file and runtime service label | Compose CLI | Yes |
| Qdrant service `qdrant` | PROVEN | Tracked Compose file and running service | Docker | Yes |
| Repository Compose file | PROVEN | `docker-compose.yml` at exact SHA | Clean worktree | Yes |
| Generated override is second | PROVEN | Runtime `config_files` label order | External override | Structurally yes |
| Interpolation env-file | PROVEN as observed operator input | Runtime `env_files` label and rollout evidence | External protected file | Path is operator input |
| Service env-file | PROVEN | Tracked Compose `env_file` contract | External protected file | Path is Compose contract |
| Image selection | PROVEN | `HERMES_IMAGE` interpolation in Compose | Existing exact image | Yes |
| Revision injection | PROVEN | `HERMES_GIT_SHA` build argument and image revision label | Exact SHA image | Yes |
| Restart policy | PROVEN | Compose and runtime inspect | Docker | Yes |
| Networks and mounts | PROVEN | Compose render and runtime metadata | Host paths/volume | Yes, with host prerequisites |
| Application-only recreate | PROVEN policy | Historical rollout contract and manifest | Separate production authority | Yes |
| Rollback image | PROVEN requirement | Historical rollout evidence | Existing local image | Operator input |
| Secrets override content shape | PROVEN | Safe YAML shape inspection: interpolation placeholders only | Interpolation env-file | Yes |
| Historical override producer | INCONCLUSIVE | No tracked script, service unit or producer evidence found | Unknown | No |
| Override creation/removal/reboot lifecycle | INCONCLUSIVE | Runtime file existence does not prove lifecycle | Unknown | No |

## Compose order

The only valid order is:

1. `<clean-worktree>/docker-compose.yml`
2. operator-supplied path named by `HERMES_SECRETS_OVERRIDE_PATH`

The second file must match the manifest's interpolation-only structure. A historical absolute worktree path is never required.

The Compose project is `hermes-agent`. No profiles are enabled. The only expected services are `hermes-bot` and `qdrant`.

## External operator inputs

These names describe protected external inputs; they are not committed values:

```text
HERMES_DEPLOY_ENV_FILE
HERMES_SERVICE_ENV_FILE
HERMES_RUNTIME_CONFIG_FILE
HERMES_SECRETS_OVERRIDE_PATH
HERMES_ROLLBACK_IMAGE
```

The target image, expected Qdrant container identity and a protected credential-fingerprint baseline are also supplied to the validator. Secret values are never command-line arguments.

## Secrets override lifecycle

The observed override is a root-owned regular file with mode `0600`. The validator requires ownership by the effective deployment user and mode `0600`. Its environment values are interpolation expressions, not embedded credentials. Its required names are documented in the tracked manifest.

However, the process that created it is not proven. Repository history, deployment scripts, runbooks, service-manager metadata and bounded rollout evidence do not establish:

- which authorized process creates it;
- when it is created or removed;
- how it is recreated after reboot;
- which operator action owns cleanup;
- whether a failed creation removes partial files.

Therefore:

```text
DEPLOYMENT_SOT_STATUS=BLOCKED
SECRETS_OVERRIDE_PRODUCER=INCONCLUSIVE
SECRETS_OVERRIDE_LIFECYCLE_PROVEN=false
GENERATOR_CREATED=false
```

Do not copy the current `/tmp` file, infer its producer from its filename, or create a new secret source. `/tmp` is ephemeral and not source-of-truth.

The unblock task must provide an operator-approved producer identity, authorized source, creation phase, owner/mode, atomic-write contract, cleanup phase and reboot recovery contract. Only then may a separate change set producer status to `proven` and add a generator.

## Read-only validator

The validator has exactly one operating mode:

```bash
python scripts/validate_hermes_deployment.py \
  --check-only \
  --manifest deploy/hermes-production.manifest.yml \
  --source-root "$DEPLOY_WORKTREE" \
  --expected-sha "$EXACT_SHA" \
  --env-file "$HERMES_DEPLOY_ENV_FILE" \
  --service-env-file "$HERMES_SERVICE_ENV_FILE" \
  --runtime-config "$HERMES_RUNTIME_CONFIG_FILE" \
  --secrets-override "$HERMES_SECRETS_OVERRIDE_PATH" \
  --credential-baseline "$PROTECTED_FINGERPRINT_BASELINE" \
  --target-image "$TARGET_IMAGE" \
  --rollback-image "$HERMES_ROLLBACK_IMAGE" \
  --expected-qdrant-id "$EXPECTED_QDRANT_ID"
```

The command is read-only. It permits only:

- `git rev-parse` and `git status`;
- `docker compose ... config --format json`;
- `docker image inspect`;
- `docker inspect` for Qdrant identity.

It never prints rendered Compose, environment values, fingerprints, configuration contents or exception bodies.

## Hard gates

The validator fails non-zero on:

- SHA mismatch or dirty worktree;
- unsupported or malformed manifest;
- missing, reordered or duplicate Compose inputs;
- missing/symlinked/malformed/wrong-mode override;
- missing or duplicate environment variables;
- literal secret values in the override;
- credential fingerprint mismatch;
- target/rollback image failure;
- image revision or entrypoint drift;
- unexpected service or Qdrant in recreate plan;
- mount, database path, network, restart or command drift;
- Household, Weekly or Shopping flag/allowlist drift;
- text or Vision provider/model drift;
- Qdrant identity drift;
- inconclusive secrets-override producer.

Warnings do not bypass these conditions.

## Current known blocker

The observed interpolation env-file contains a duplicated variable name. Values were not read or recorded. The validator rejects duplicate names because last-value-wins behavior is not a reproducible credential contract.

This task does not modify that file. An independently authorized production-config task must resolve the duplicate and create a protected fingerprint baseline before deployment can pass.

## Credential fingerprint baseline

The protected baseline is external YAML with this shape:

```yaml
version: 1
algorithm: sha256
fingerprints:
  VARIABLE_NAME: <64-lowercase-hex>
```

It must contain exactly the credential names required by the manifest. The validator computes comparisons in memory and prints only pass/fail. Fingerprint values themselves must not be copied into evidence or a pull request.

## Application-only recreate contract

After all read-only gates pass and separate production authority is granted, the planned recreate set must contain only:

```text
hermes-bot
```

`qdrant` must never appear in that set. Do not use full-project `up`, `down` or restart commands. The exact production command belongs to a separately approved rollout task.

## Post-deploy gates

A future authorized rollout must verify:

- exact image and revision;
- Hermes running with restart count zero;
- unchanged Qdrant identity and restart count;
- SQLite integrity and foreign-key checks;
- unchanged disabled feature flags and provider routing;
- privacy-safe startup log delta;
- manual Telegram smoke at the specified stop point.

Rollback is required for runtime instability, data-integrity failure, provider/routing drift, privacy leakage or Qdrant change. Database restoration requires separate evidence and authority.

## Actions requiring separate authority

- creating or modifying protected env/config/fingerprint files;
- establishing the override producer;
- building or tagging an image;
- creating backups;
- recreating `hermes-bot`;
- restoring an image or database;
- changing provider/model, feature flags, Telegram or Qdrant.

Until the producer, duplicate env name and fingerprint baseline are resolved, the deployment source-of-truth is intentionally **PARTIAL** and rollout is **BLOCKED**.
