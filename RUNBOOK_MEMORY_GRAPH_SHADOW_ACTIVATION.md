# RUNBOOK: Memory Graph Shadow Activation

**WARNING:** This runbook describes the future production SHADOW activation. It does **not** authorize activation on its own.

## Phase 0 — Canonical Provenance
1. Verify exact canonical `main` SHA.
2. Confirm `GRAPH_SERVE_MODE_AVAILABLE=false`.
3. Verify no unstaged local changes exist.

## Phase 1 — Exact Image Verification
1. Ensure the target Docker image is built and attested.
2. Run `scripts/check_memory_graph_shadow_readiness.py` preflight.

## Phase 2 — Current Production Health
1. Verify SQLite integrity (`PRAGMA integrity_check`).
2. Verify foreign key integrity (`PRAGMA foreign_key_check`).
3. Check Qdrant availability and Telegram gateway health.

## Phase 3 — Maintenance/Writer Safety
1. Pause upstream queues.
2. Prove `WRITER_STATE=QUIESCED`. No transactions may be in flight.

## Phase 4 — Fresh Verified Backup
1. Trigger standard Hermes backup process.
2. Verify backup SHA256 matches expectation.
3. Validate backup SQLite integrity.

## Phase 5 — Staged Graph Schema Migration
1. Copy production DB to private staging path.
2. Run canonical staged schema migration on the staged copy.
3. Confirm `ABSENT` or `KNOWN_COMPATIBLE_PARTIAL` becomes `CURRENT`. (Fail-closed on `INCOMPATIBLE`).

## Phase 6 — Staged DB Integrity Validation
1. Run `PRAGMA integrity_check` on the staged database.
2. Run `PRAGMA foreign_key_check` on the staged database.
3. Confirm authoritative rows were unchanged.

## Phase 7 — Atomic Publish
1. Replace production database atomically with the staged database.
2. Capture rollback artifact reference.

## Phase 8 — Enable SHADOW
1. Set `MEMORY_GRAPH_MODE=shadow` in production `.env`.
2. Ensure `GRAPH_CONTEXT_SERVED_TO_USERS=false`.

## Phase 9 — Exact-Image Startup
1. Start containers using the verified exact image.
2. Monitor startup logs for immediate crashes.

## Phase 10 — Health/Canary Observation
1. Observe for a defined operator window (e.g., 2 hours).
2. Gather Shadow Health Receipt.
3. Ensure no `integrity_block_count` and no unexpected mutations.

## Phase 11 — PASS or Rollback
1. If Health Receipt is PASS, proceed to SOT closure.
2. If FAIL, perform exact Rollback (Database + Image state).

## Phase 12 — SOT Closure
1. Update `docs/CURRENT_STATE.md` to reflect `GRAPH_SHADOW_PRODUCTION_ACTIVATED=true`.
2. STOP before any serve-mode activation.
