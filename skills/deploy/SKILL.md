---
name: deploy
description: Use for fail-closed HealBite production deployments.
---

# HealBite Production Deploy Skill

Use this skill to prepare, execute, verify, or roll back an authorized HealBite production deployment. It coordinates the repository's canonical deployment wrapper and preserves separate approval gates for image publication, database migration, feature activation, and credential changes.

## When to Use

- The operator explicitly asks for a production deploy, deploy plan, rollback, or deploy-readiness audit.
- A release must be bound to an exact source SHA and immutable image identity.
- A production SQLite migration needs backup, rehearsal, publish, and recovery gates.
- Do not use this skill for ordinary development, a docs-only PR, registry publication alone, or an unapproved restart.

## Prerequisites

- Read `AGENTS.md`, `docs/CURRENT_STATE.md`, and the [production deployment contract](../../docs/runbooks/hermes-production-deployment.md) completely.
- Use a clean worktree created from the approved project-main SHA. Preserve any dirty canonical checkout.
- Obtain explicit authorization for every production mutation. A successful plan never authorizes execution.
- Know the exact 40-character source SHA, immutable image ID or digest, approved secret-source path, live SQLite mount, and rollback image before mutation.
- Keep backups and evidence outside the repository with restrictive permissions. Never print secret values, database rows, Telegram identities, or Qdrant payloads.
- Use `terminal` for the repository wrappers, `read_file` for contracts, and `search_files` to locate related migration code and tests.

## How to Run

Start with repository and plan modes; they are read-only with respect to production:

```bash
scripts/hermes_production_deploy.sh check-repository \
  --expected-sha <exact-40-character-source-sha>

scripts/hermes_production_deploy.sh plan \
  --secret-source /etc/hermes/hermes-production.env \
  --image <immutable-image-id-or-digest> \
  --revision <exact-40-character-source-sha>
```

Run `execute-deploy` or `execute-rollback` only inside a separately authorized rollout task and only after every prerequisite below passes.

## Quick Reference

| Gate | Required evidence | Mutation allowed |
| --- | --- | --- |
| Source | exact SHA, trusted remote, clean worktree | No |
| Image | immutable ID/digest and matching OCI revision | No |
| Plan | canonical Compose render, capacity, secret metadata | No |
| Backup | SQLite backup API, checksum, restore test | Backup artifact only |
| Migration | rehearsed additive change on staging copy | Only with migration approval |
| Deploy | explicit confirmation and rollback readiness | `hermes-bot` recreate only |
| Verify | stable runtime, zero unauthorized DB/Qdrant delta | No |

The [trusted-source predeploy contract](../../docs/runbooks/TRUSTED_SOURCE_ENV_VISION_PREDEPLOY.md) defines provenance and environment-precedence checks. The [weekly/shopping rollout runbook](../../docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md#backup-contract-before-first-production-ddl) is the detailed source for SQLite backup, staged migration, and emergency restore constraints.

## Procedure

1. **Freeze scope and success criteria.** Record the requested source SHA, image, services, database migration, feature flags, and explicit stop point. Treat build, registry publication, migration, deploy, feature activation, secret changes, and smoke tests as separate gates.
2. **Verify repository provenance.** Fetch the HealBite project remote, resolve its main SHA, and require the worktree HEAD to equal the approved SHA. Require a clean status and a matching trusted remote. Never repair a dirty checkout with reset, clean, stash, rebase, or file copying.
3. **Run code gates.** Run focused tests through `scripts/run_tests.sh`, then `bash scripts/agent_check.sh`, `git diff --check`, and the full `scripts/run_tests.sh` suite required by repository policy. Stop on any unexplained failure.
4. **Verify the immutable image.** Require a local immutable image ID or repository digest. Require its single `org.opencontainers.image.revision` label to equal the exact source SHA. Reject tags as provenance.
5. **Collect a sanitized production baseline.** Confirm service identity, container state and restart count, exact SQLite mount, integrity and foreign-key status, Qdrant health, feature-state fingerprints, and protected-secret key fingerprints. Record only safe classifications, hashes, and counts.
6. **Prepare rollback before mutation.** Validate the previous immutable image and its revision, canonical Compose render, protected-secret transition, capacity, and the health contract the rollback must satisfy.
7. **Back up SQLite before first DDL.** Use the SQLite backup API or an approved online equivalent, not a plain copy of an active database. Store a timestamped immutable backup outside the live DB directory and repository, record its SHA-256, verify `PRAGMA integrity_check`, restore it to a separate temporary path, and verify the restored copy.
8. **Rehearse the migration.** Apply the additive initializer to a production-derived staging copy. Verify schema/index fingerprints, `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, expected safe aggregates, preservation of unrelated tables, and idempotency on a second pass. Do not print rows or identifiers.
9. **Publish the migration only under its own approval.** Quiesce writers, re-confirm the source database identity, use the approved staged-copy/atomic-publish mechanism, and stop on any uncertain publish state. Retain the backup, manifest, and displaced staging artifact when recovery may be needed. Never perform manual DDL repair in the deploy task.
10. **Re-run the deploy plan.** The canonical wrapper must independently validate source, image label, secret source, Compose render, DB mount, capacity, baseline, and rollback readiness immediately before mutation.
11. **Execute the authorized deploy.** Use the canonical `execute-deploy` confirmation. Recreate only `hermes-bot` with the exact inspected image. Do not recreate Qdrant or change feature flags, secrets, or database schema unless those changes were separately authorized.
12. **Verify the post-state.** Require stable samples, expected revision/image, `restart_count=0`, no traceback, Telegram connectivity without sending user messages, unchanged protected-secret fingerprints, SQLite integrity, expected migration state, no unauthorized DB delta, and Qdrant non-interference.
13. **Close with evidence.** Report exact source and image identities, safe checks/counts, backup verification, migration classification, deploy/rollback status, and remaining risks. Clean only the canonical ephemeral override through the wrapper's cleanup mode.

## Failure/Rollback

- Fail closed before mutation when source, image, backup, capacity, secret, Compose, DB mount, migration rehearsal, or rollback readiness is uncertain.
- A failed migration blocks deployment. Do not retry automatically, repair with ad-hoc DDL, drop tables, or delete recovery artifacts.
- After deploy mutation, let the canonical orchestrator restore the previous protected override and make its single approved recreate attempt with the previous immutable image.
- Application rollback keeps a successfully applied additive schema. Do not restore the pre-migration database merely to roll back an image.
- Restore a database only for confirmed corruption, with explicit operator authorization and an accepted data-loss window. Stop writers, preserve the failed DB, atomically restore the verified backup, then verify integrity and foreign keys before starting the approved image.
- A healthy automatic rollback reports `ROLLED_BACK`, never `PASS`. A failed rollback reports `FAIL` and preserves both the original and rollback failure classifications.
- If evidence writing fails after mutation, classify the deployment as unverified and invoke the same rollback path.

## Pitfalls

- Treating a mutable tag, abbreviated SHA, or tag text as release provenance.
- Building from the dirty canonical checkout or copying files from it.
- Using a plain file copy for an active SQLite database.
- Combining database migration, application rollback, Qdrant mutation, feature enablement, or secret rotation under one implicit approval.
- Rendering full Compose output, printing dotenv content, or retaining raw logs as evidence.
- Declaring `PASS` after a rollback or after any unresolved post-mutation check.

## Verification

- [ ] Worktree HEAD equals the approved 40-character project SHA and status is clean.
- [ ] Immutable image identity and OCI revision match the source SHA.
- [ ] Focused checks, `scripts/agent_check.sh`, `git diff --check`, and required full tests passed.
- [ ] Backup checksum, integrity check, and isolated restore test passed before DDL.
- [ ] Migration rehearsal passed twice and preserved unrelated state.
- [ ] Previous image, protected secret state, capacity, and rollback health contract were proven before mutation.
- [ ] Post-deploy container, Telegram, SQLite, and Qdrant checks passed with no unauthorized delta.
- [ ] Evidence is sanitized and the final status distinguishes `PASS`, `ROLLED_BACK`, `FAIL`, and `BLOCKED`.
