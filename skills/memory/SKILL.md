---
name: memory
description: Use for HealBite Memory OS or Qdrant operations.
---

# HealBite memory router

Use this skill for Memory OS recall/convergence defects, SQLite Memory facts,
Qdrant health or rebuild work, fact UUID/epoch safety, SQLite restore, or
authorized Memory migration. Do not use it for ordinary application changes,
raw production fact inspection, or unapproved production mutation.

SQLite remains the source of truth. Qdrant is a rebuildable semantic index.

## When to Use

Use only for the Memory OS and Qdrant conditions named above. Select the
smallest relevant reference set below.

## Select references

| Task | Read |
| --- | --- |
| Ordinary Memory bug or hydration/isolation issue | [authority](references/authority.md), [convergence](references/convergence.md) |
| Fact UUID, legacy epoch, or migration identity | [epoch safety](references/epoch-safety.md), authority |
| Qdrant health, dry run, or authorized rebuild | [Qdrant rebuild](references/qdrant-rebuild.md), authority |
| SQLite restore/rollback or timeline recovery | [restore](references/restore.md), epoch safety, Qdrant rebuild |

## Procedure

Start read-only: resolve the actual runtime SQLite path, collection, and
feature state; capture integrity/FK and safe aggregate health; then use the
smallest relevant test or dry run. For example:

```bash
MEMORY_VECTOR_ENABLED=false \
  venv/bin/python scripts/rebuild_qdrant_memory_index.py \
  --db-path <resolved-live-or-rehearsal-db> --dry-run
```

`--dry-run` contacts no Qdrant. The current rebuild is upsert-only and does not prove identity equality or stale-point deletion.

## Failure/Rollback

Stop on path ambiguity, integrity/FK failure, cross-user evidence, Qdrant schema
drift, or unexpected writes. SQLite restore, Qdrant writes/deletes, replacement,
cutover, configuration change, and restart need explicit separate authority.

## Completion

Report only safe classifications, fingerprints, and aggregates. Never include
fact values, user/household/Telegram IDs, secrets, or Qdrant payloads.
