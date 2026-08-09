# Production Readiness Checklist

Status: fail-closed release review aid

This checklist classifies readiness; it does not authorize production
mutation. The authoritative procedure is `skills/deploy/SKILL.md` together with
the versioned deployment policy and applicable runbook. A plan or green CI run
is evidence only, never deploy authority.

Record every applicable technical gate as `PASS`, `FAIL`, `UNKNOWN`,
`INCONCLUSIVE`, or `NOT_APPLICABLE`. Any required technical result other than
explicit `PASS` blocks mutation.

## 1. Authority and scope

- [ ] The task explicitly authorizes the requested production mutation and
  names the last allowed action.
- [ ] Build, registry publication, migration, deploy, feature activation,
  credential change, Qdrant mutation, and live smoke are separately classified.
- [ ] The affected services, database path, migration components, feature state,
  and forbidden mutation classes are exact.
- [ ] Governance warnings are reported separately and do not replace or waive a
  technical gate.

## 2. Exact source and image

- [ ] Canonical repository/remote/main provenance is verified in a clean
  isolated worktree.
- [ ] The exact 40-character deployment SHA is locked and equals the approved
  canonical main SHA.
- [ ] The build used the exact trusted Git tree and a clean, secret-free build
  context.
- [ ] The candidate is addressed by immutable registry digest or inspected local
  image ID, never a mutable tag.
- [ ] The image's single OCI revision equals the exact deployment SHA.
- [ ] Canonical image scanning covers metadata/history, every recoverable layer,
  and the final filesystem with a valid image-bound receipt.

## 3. Tests and release evidence

- [ ] Focused, adjacent, project, diff, and exact-head CI checks passed for the
  exact candidate SHA.
- [ ] CI event, repository, branch/head SHA, attempt, and required jobs are
  unambiguous and successful.
- [ ] Registry/build/image receipts agree on source SHA, tree, immutable digest,
  platform, OCI revision, ordered layers, and scan-policy identity.
- [ ] Secret findings are zero after only exact evidence-bound approved policy;
  missing or malformed evidence is not interpreted as zero.

## 4. Current production baseline

- [ ] Fresh read-only discovery proves the intended host, Compose project,
  service identity, mounts, image, restart count, capacity, and feature state.
- [ ] Protected secret source path, owner, mode, file type, closed key set, and
  value fingerprints are valid without outputting values.
- [ ] `hermes-bot` and required dependencies are healthy before mutation.
- [ ] Telegram diagnostics use `getMe`, updater state, or synthetic no-send
  checks; no live user message is sent without explicit smoke authority.
- [ ] Qdrant identity, storage, health, and non-interference baseline are
  captured without reading payloads.

## 5. SQLite backup and migration

- [ ] Active writers are exactly zero before protected state capture or DDL, and
  the required lease remains held.
- [ ] A fresh backup is taken from the exact live DB through the SQLite backup
  API or approved online equivalent before migration.
- [ ] Backup SHA-256, timestamp, source identity, integrity/FK checks, and an
  isolated restore test are all PASS.
- [ ] Pre-migration SQLite integrity is `ok` and foreign-key violations are zero.
- [ ] Migration runs only through the canonical staged path; no manual DDL or
  direct initializer substitutes for it.
- [ ] The ordered expected mutation subset exactly equals the read-only derived
  effective subset at planning and immediately before first DDL.
- [ ] Rehearsal is idempotent, preserves unrelated state, and proves the exact
  target schema fingerprint.

## 6. Rollback readiness

- [ ] A clear rollback path exists before mutation, with exact triggers and
  outcome classification.
- [ ] The previous immutable image is locally available, identity-bound, and
  has a valid OCI revision or uses the separately authorized legacy bootstrap.
- [ ] The previous image is compatible with the rehearsed post-migration schema.
- [ ] Protected-secret transition, Compose render, capacity, and rollback health
  contract are proven.
- [ ] Automatic rollback is image-only and reports `ROLLED_BACK`, never `PASS`.
- [ ] Database restore is a separate explicit authority with an accepted data
  loss window; it is not an implicit application rollback.

## 7. Deployment and post-state

- [ ] The canonical deploy plan is rerun immediately before execution and every
  required technical gate remains PASS.
- [ ] Execution recreates only the authorized service with the exact inspected
  immutable image; Qdrant, DB, features, and secrets remain unchanged unless
  separately authorized.
- [ ] Health checks pass across stable samples: expected image/revision,
  `restart_count=0`, bounded clean startup logs, Telegram connectivity/no-send,
  SQLite integrity, and required dependency health.
- [ ] Post-state evidence proves protected secrets unchanged, expected schema
  state, no unauthorized SQLite delta, and Qdrant non-interference.
- [ ] Evidence is written successfully and sanitizes secrets, user data, raw
  logs, messages, and identifiers.

## Readiness outcome

```text
PRODUCTION_READINESS=PASS|FAIL|BLOCKED|INCONCLUSIVE
SOURCE_SHA=
IMMUTABLE_IMAGE=
OCI_REVISION=
EXACT_HEAD_CI=
IMAGE_SECRET_SCAN=
ACTIVE_WRITERS=
BACKUP_AND_RESTORE=
SQLITE_INTEGRITY_FK=
MIGRATION_SCOPE=
ROLLBACK_READY=
HEALTH_CHECKS=
PRODUCTION_AUTHORITY=
BLOCKING_ISSUES=
```
