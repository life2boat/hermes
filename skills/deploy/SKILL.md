---
name: deploy
description: Use for authorized HealBite production deployment work.
---

# HealBite deployment router

Use this skill only for production deploy/readiness, image qualification,
database migration, backup/restore, rollback, or untrusted-runtime recovery.
Do not use it for ordinary development, a docs-only PR, registry publication
alone, or an unapproved restart.

## When to Use

Use only when the task itself crosses a production, release-readiness, database,
or rollback boundary. Select the smallest relevant reference set below.

## Select references

| Task | Read |
| --- | --- |
| Readiness or image qualification only | [provenance](references/provenance.md), [image attestation](references/image-attestation.md) |
| Deploy without schema change | provenance, [authority](references/authority.md), [rollback](references/rollback.md), [runtime verification](references/runtime-verification.md) |
| Migration or schema compatibility | [database safety](references/database-safety.md), [migration](references/migration.md), [backup/restore](references/backup-restore.md), rollback |
| Rollback or untrusted-runtime recovery | rollback, database safety, runtime verification |

Read the canonical detail only for the selected operation:
[`docs/runbooks/hermes-production-deployment.md`](../../docs/runbooks/hermes-production-deployment.md).
The weekly/shopping migration runbook owns authority-package command sequencing.

## Procedure

Start with the read-only canonical wrapper modes:

```bash
scripts/hermes_production_deploy.sh check-repository \
  --expected-sha <exact-40-character-source-sha>

scripts/hermes_production_deploy.sh plan \
  --secret-source /etc/hermes/hermes-production.env \
  --image <immutable-image-id-or-digest> \
  --revision <exact-40-character-source-sha>
```

Use `execute-deploy`, `execute-rollback`, migration publication, restore, or
legacy bootstrap only when the task explicitly authorizes that mutation and all
selected technical gates are `PASS`. A plan never grants execution authority.

## Failure/Rollback

Stop before mutation if a required source, image, secret, database, migration,
capacity, health, or rollback gate is `FAIL`, `UNKNOWN`, missing, or ambiguous.
The canonical orchestrator owns its one approved rollback attempt; a recovered
runtime reports `ROLLED_BACK`, never `PASS`.

## Completion

Report exact source/image identities, sanitized evidence, mutation scope,
post-state, rollback status, and the next authorized action. Never print
secrets, rows, identifiers, payloads, raw logs, or rendered Compose output.
