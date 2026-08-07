---
name: memory
description: Use for SQLite maintenance and Qdrant reconciliation.
---

# Hermes Memory Maintenance Skill

Use this skill to inspect, back up, rebuild, or reconcile HealBite Memory OS safely. SQLite remains the source of truth; Qdrant is a rebuildable semantic index and must never become the authority for user facts.

## When to Use

- Memory recall is missing, stale, duplicated, or inconsistent with SQLite.
- An operator requests a Memory OS health check, SQLite backup, Qdrant rebuild, or SQLite-only fallback.
- A database migration or incident requires integrity and reconciliation evidence.
- Do not use this skill to inspect raw production facts, reveal user identifiers, or change production without explicit authorization.

## Prerequisites

- Read `AGENTS.md`, `docs/CURRENT_STATE.md`, `gateway/platforms/healbite_memory_bridge.py`, and `scripts/rebuild_qdrant_memory_index.py` before changing behavior.
- Resolve the actual SQLite path and Qdrant collection from runtime mounts/configuration; do not assume a copied path is live.
- Use `terminal` for the repository diagnostics and rebuild script, `read_file` for implementation contracts, and `search_files` for callers/tests.
- Keep database backups outside the repository with mode `0700` on directories and `0600` on files.
- Obtain explicit approval before DB writes, Qdrant writes/deletes, feature-flag changes, restarts, restores, or collection replacement.

## How to Run

Begin with a read-only status snapshot and a dry run:

```bash
./scripts/healbite status

MEMORY_VECTOR_ENABLED=false \
  venv/bin/python scripts/rebuild_qdrant_memory_index.py \
  --db-path <resolved-live-or-rehearsal-db> \
  --dry-run
```

Do not run the non-dry rebuild against production until Qdrant mutation is explicitly authorized and rollback is ready.

## Quick Reference

| Component | Role | Safe default |
| --- | --- | --- |
| SQLite `memory_os_facts` | authoritative facts | read-only inspection |
| SQLite FTS | local derived search index | rebuild only with DB approval |
| Qdrant collection | derived semantic index | health/count metadata only |
| Rebuild script | upserts SQLite facts into Qdrant | `--dry-run` |
| Vector feature flag | selects semantic recall | leave unchanged |

The current rebuild implementation is **upsert-only**. A successful rebuild repopulates current SQLite facts but does not prove that stale Qdrant-only points were removed.

## Safety Decision Memory

### User and household isolation

**Invariant:** Scope every Memory OS fact read and write by normalized `user_id`. If a maintenance task also touches household-owned product data, resolve access through the authoritative household authorization context rather than trusting a raw household, member, user, or Telegram identifier.

**Why:** User and household boundaries protect both confidentiality and write ownership. A transport identifier or caller-supplied domain ID is not proof that the caller may read or mutate that scope.

**Evidence:** Require user-scoped SQLite predicates, Qdrant `user_id` filters/payloads, SQLite re-hydration of semantic hits, and focused cross-user/cross-household denial tests. Evidence remains aggregate-only and contains no identifiers.

### Durable state and derived search

**Invariant:** Keep SQLite as the durable source of truth wherever the current Memory OS contract applies; treat Qdrant and SQLite FTS as derived search indexes.

**Why:** Vector points can be missing, stale, duplicated, or unavailable without changing the authoritative fact. Allowing a derived index to overwrite SQLite would turn search-index drift into durable data loss.

**Evidence:** Verify Qdrant hits are hydrated from user-scoped SQLite rows, SQLite-only fallback returns authoritative facts, and SQLite schema/content fingerprints remain unchanged during a Qdrant-only operation.

### Dual-write and reconciliation

**Invariant:** Commit the SQLite fact first and only then schedule the derived Qdrant upsert. Treat the two writes as non-atomic and retain reconciliation as an explicit maintenance responsibility.

**Why:** The current dual-write path crosses two stores and may complete in SQLite before an asynchronous Qdrant upsert succeeds. Deletes also do not remove stale Qdrant points, so a successful write or equal count alone cannot prove convergence.

**Evidence:** Compare SQLite candidate identities with hydrated Qdrant identities, record candidate/upsert counts and adapter failures, and keep the stale-point classification visible after every dry run or rebuild.

### Qdrant mutation boundary

**Invariant:** Default to read-only metadata and `--dry-run`; permit only scoped upserts under explicit authorization. Replacement, cutover, deletion, or collection cleanup requires its own reviewed workflow and rollback point.

**Why:** The live collection is shared derived state, and the current rebuild is upsert-only. In-place deletion based on counts can remove valid points or cross an ownership boundary without proving which identities are stale.

**Evidence:** Pin the resolved URL, collection, vector size, DB path, and optional user scope; retain dry-run candidates, post-upsert readiness/counts, hydration tests, and proof that no delete or collection switch occurred unless separately authorized.

### Integrity around state changes

**Invariant:** Resolve the exact live SQLite path, validate integrity and foreign keys before and after every state-changing operation, and prepare a verified backup before SQLite mutation.

**Why:** A guessed path can silently create a new empty database, while corruption or FK violations can be propagated into backups, migrations, FTS rebuilds, or Qdrant reconciliation.

**Evidence:** Record path/device/inode, pre/post `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, schema/user-version fingerprints, backup SHA-256, and a successful isolated restore test.

## Procedure

1. **Define the failure.** Separate missing recall, wrong-user recall, count drift, Qdrant unavailability, SQLite corruption, FTS degradation, and configuration drift. Record only safe error classes.
2. **Resolve authoritative paths.** Inspect runtime configuration and mounts to identify the live SQLite DB, Qdrant URL, collection, vector size, and feature-flag state without printing secrets.
3. **Capture a read-only baseline.** Run `./scripts/healbite status`. Record SQLite integrity, foreign-key violations, schema/user version, safe aggregate fact count, Qdrant readiness, collection vector settings, safe point count, and service restart counts.
4. **Validate user isolation.** Confirm every SQLite access is scoped by normalized `user_id` and Qdrant payload hydration re-checks ownership in SQLite. Never copy identifiers or payloads into reports.
5. **Back up before SQLite mutation.** Use the SQLite backup API or approved online equivalent. Record checksum, run `PRAGMA integrity_check`, restore into a separate temporary database, and re-check integrity. Do not use a plain copy while writers are active.
6. **Run a dry rebuild.** Keep `MEMORY_VECTOR_ENABLED=false` and use `--dry-run` against the resolved database or rehearsal copy. Confirm the candidate count matches the expected SQLite scope without contacting Qdrant.
7. **Classify reconciliation.** A lower Qdrant count suggests missing derived points. An equal count does not prove identity equality. A higher count or known deletes suggests stale points because the rebuild path upserts and does not delete orphans.
8. **Choose the least-mutating repair.** For missing points, an authorized rebuild can upsert SQLite facts. For suspected stale points or schema/vector-size drift, prepare a separately approved replacement-collection workflow instead of deleting the live collection in place.
9. **Rebuild only with approval.** Pin the resolved Qdrant URL, collection, vector size, and DB path. Keep logs aggregate-only. Stop on adapter errors, negative results, unexpected candidate counts, or configuration drift.
10. **Verify exact behavior.** Re-run safe counts and readiness, sample retrieval through the application hydration path, confirm user isolation, and verify SQLite fingerprints did not change during a Qdrant-only repair.
11. **Use SQLite-only fallback when needed.** With explicit configuration and restart authorization, disable vector search and keep Qdrant running. Verify FTS/LIKE recall and service health. Do not delete Qdrant as part of fallback.
12. **Report and retain recovery evidence.** Record source DB identity, backup verification, candidate/upsert counts, collection metadata, fallback state, and unresolved stale-point risk without raw facts or identifiers.

## Failure/Rollback

- Stop on SQLite integrity failure, foreign-key violations, path ambiguity, cross-user evidence, Qdrant schema mismatch, or unexpected writes.
- A Qdrant-only failure must not trigger a SQLite restore. Keep SQLite authoritative and fall back to SQLite search when authorized.
- If an authorized replacement collection fails before cutover, leave the live collection and feature flag unchanged.
- If cutover fails, revert the collection/config pointer to the previously verified collection; do not delete either collection until a separate cleanup approval.
- Restore SQLite only for confirmed corruption and only from a verified backup after writers are stopped and the operator accepts the recovery point.

## Pitfalls

- Assuming equal SQLite and Qdrant counts prove reconciliation.
- Treating the upsert rebuild as deletion of stale Qdrant points.
- Reading or reporting raw memory values, user IDs, Telegram IDs, or vector payloads.
- Passing a guessed DB path and accidentally creating a new empty SQLite file.
- Enabling vector search, replacing a collection, or restarting the bot as part of diagnosis without approval.
- Letting a Qdrant outage mutate or downgrade SQLite facts.

## Verification

- [ ] Live SQLite path and Qdrant collection were resolved from runtime state.
- [ ] SQLite integrity and foreign-key checks passed.
- [ ] Backup checksum and isolated restore test passed before any DB mutation.
- [ ] Dry-run candidates matched the expected safe SQLite scope.
- [ ] Rebuild/replacement behavior accounted for the upsert-only stale-point limitation.
- [ ] Post-repair application hydration preserved user isolation.
- [ ] SQLite remained unchanged during a Qdrant-only operation.
- [ ] No secret, identifier, raw fact, or Qdrant payload appeared in evidence.
