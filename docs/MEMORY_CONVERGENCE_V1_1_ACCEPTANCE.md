# Memory Convergence v1.1 Acceptance Contract

## Scope and authority

This contract accepts repository behaviour only. SQLite is authoritative;
Qdrant is a derived, rebuildable semantic index. Repository implementation is
not evidence that production was migrated or that vector mode is active.

## Canonical staged migration contract

The ordered canonical registry now contains exactly one `memory_convergence`
component after `fridge_menu`. Its stdlib-only schema authority is
`gateway/memory/schema.py`; both the repository migration CLI and safe
development/test initialization consume that contract. Production runtime
initialization validates the staged schema read-only and fails closed when the
component is not `CURRENT`; it does not substitute startup DDL for the staged
migration.

The component classifies `ABSENT`, a closed set of additive
`KNOWN_COMPATIBLE_PARTIAL` layouts, `CURRENT`, and all other layouts as
`INCOMPATIBLE`. Migration adds `vector_revision INTEGER NOT NULL DEFAULT 1`,
the durable outbox/meta tables and their exact constraints/indexes, then seeds
one `UPSERT` intent per legacy fact before setting the completion marker in the
same SQLite transaction. Seed rows contain owner, canonical fact id, revision,
operation and scheduling metadata only; fact text, embeddings, prompts,
provider output and secrets are never copied. No Qdrant or provider code is
imported or called by the migration.

Repository status is `IMPLEMENTED_IN_REPOSITORY`. It is not
`MIGRATED_IN_PRODUCTION` or `ACTIVE_IN_PRODUCTION`.

The normal gateway owns one bounded reconciliation task per process when
`MEMORY_VECTOR_ENABLED=true`. It performs an immediate startup tick and then a
bounded periodic tick. When the flag is false it does not open/create the
database and does not contact Qdrant. Each tick is limited to 25 operations and
two seconds between individual bounded client calls; the interval defaults to
60 seconds and is clamped to 5..3600 seconds. Durable outbox recovery, not
best-effort task completion, is the shutdown guarantee.

## Independent acceptance matrix

| ID | Obligation | Deterministic evidence |
|---|---|---|
| AC-01 | SQLite remains canonical | hydration and fallback tests |
| AC-02 | fact mutation and vector intent are atomic | transaction fault injection |
| AC-03 | committed relevant mutation retains durable intent | restart test |
| AC-04 | delete creates durable derived delete | outage/delete test |
| AC-05 | retry survives restart | retry recovery test |
| AC-06 | duplicate UPSERT is safe | idempotent replay and two-worker tests |
| AC-07 | duplicate DELETE is safe | delete replay tests |
| AC-08 | late UPSERT cannot override a newer revision | revision tests |
| AC-09 | late UPSERT after delete cannot resurrect | delete correction test |
| AC-10 | late DELETE cannot remove recreated identity | generation test |
| AC-11 | owner mismatch fails closed | poisoned owner test |
| AC-12 | malformed intent cannot contact Qdrant | poison test |
| AC-13 | failed Qdrant call is not success | outage tests |
| AC-14 | weak/missing acknowledgement remains unresolved | adapter ACK test |
| AC-15 | attempt limit is observable as BLOCKED | retry-limit test |
| AC-16 | outage leaves durable backlog | outage tests |
| AC-17 | retry has bounded backoff and no busy loop | injected clock tests |
| AC-18 | batch size is bounded | worker bound test |
| AC-19 | tick wall time is bounded | injected perf-counter test |
| AC-20 | shutdown never discards durable intent | runtime shutdown tests |
| AC-21 | startup and periodic ticks recover pending work | runtime lifecycle tests |
| AC-22 | status exposes aggregate health without content | health/redaction tests |
| AC-23 | vector-off mutations retain future intent without Qdrant calls | flag tests |
| AC-24 | additive legacy migration is idempotent | migration rerun tests |
| AC-25 | historical unknown orphans are not reported absent | offline classifier tests |
| AC-26 | overlapping workers converge safely | barrier tests |
| AC-27 | late worker failure cannot undo another worker ACK | mixed-result race test |
| AC-28 | repair is explicit, owner-scoped, bounded and idempotent | repair tests |
| AC-29 | stale client can recover on a later due tick | client recovery tests |
| AC-30 | machine-readable alert state distinguishes WATCH/ALERT | health tests |
| AC-31 | transient SQLite lock cannot erase backlog | busy-lock test |
| AC-32 | historical classification never authorizes deletion | classifier contract |

## Fault-injection map

The provider-free focused suite deterministically covers F01--F25:

- F01 atomic enqueue exception rolls back the fact transaction;
- F02 restart processes a committed untouched intent;
- F03/F04 failed UPSERT/DELETE retain retryable durable work;
- F05 success before local ACK replays the idempotent operation;
- F06/F07 duplicate UPSERT/DELETE converge safely;
- F08/F09/F10 stale update/delete generation races are corrected;
- F11 restart with backlog converges;
- F12/F13 malformed and cross-owner rows block before Qdrant;
- F14 one failed item does not prevent a later item;
- F15 client initialization failure is retried with a fresh client;
- F16 max attempts becomes observable BLOCKED work;
- F17 repair requires exact owner and bounded operation identities;
- F18/F19 disabled mode stores intent and later enable converges it;
- F20 two barrier-synchronized workers safely deliver one operation;
- F21 injected first-item adapter exception preserves later progress;
- F22 a transient SQLite exclusive lock raises boundedly and preserves backlog;
- F23 runtime shutdown leaves durable pending state recoverable;
- F24 startup immediately examines due work while future retry/BLOCKED rows stay
  governed by their state and schedule;
- F25 fake current, missing, foreign, malformed, duplicate, stale-revision and
  non-current-identity points receive read-only classifications.

No test sleeps for retry time; injected clocks, barriers and fake adapters make
failure order deterministic.

## Review-concern classification

| Concern | Classification on v1.1 | Evidence |
|---|---|---|
| Worker code may exist without runtime ownership | REAL_GAP, CLOSED | gateway lifecycle owns `MemoryVectorRuntime` |
| Startup-only/opportunistic work may stall without later writes | REAL_GAP, CLOSED | immediate plus periodic bounded tick |
| Concurrent ticks may corrupt state | REVIEW HYPOTHESIS, PROVEN CLOSED | conditional ACK/failure updates and barrier tests |
| Aggregate status lacks alertability | PARTIAL, CLOSED | `alert_status`, closed reasons, last reconciliation |
| BLOCKED repair can reset all tenants | REAL_GAP, CLOSED | explicit owner plus max-25 operation IDs |
| Historical unknown Qdrant orphans may exist | REAL, NOT DESTRUCTIVELY CLOSED | read-only classifier; no scan/delete |
| Production migration/activation has happened | NOT APPLICABLE | repository-only task; not performed |

## Operational health

The gateway runtime-status JSON contains a `memory_vector` object. Its closed
states are `DISABLED`, `NOT_STARTED`, `CONVERGED`, `PENDING`, `DEGRADED`, or
`BLOCKED`. Convergence status includes aggregate counts, oldest age,
`last_success_at`, `last_reconciliation_at`, closed `last_error_class`, and
counters. It contains no fact text, vector, prompt, payload, identifier, or
secret.

`alert_status` is:

- `OK` for healthy convergence/disabled state;
- `WATCH` for a young pending/retry backlog;
- `ALERT` when blocked work exists, unresolved age reaches twice the maximum
  retry backoff (600 seconds), or the enabled reconciler has not ticked for the
  same interval.

These are machine-readable repository signals, not a paging service.

## Blocked repair

Repair accepts only positive internal outbox operation IDs, at most 25, plus
one normalized owner. It never accepts a Qdrant point ID. Selection requires
both `state='BLOCKED'` and exact owner/id match. Processing re-reads SQLite and
revalidates owner, existence and revision. A missing canonical row causes the
scoped delete path; repair cannot restore deleted fact content. A second repair
after successful ACK selects nothing.

## Historical orphan classification

`classify_historical_points` is offline/read-only and returns only aggregate
counts and ordinal classifications: `CANONICAL_MATCH`, `MISSING_IN_SQLITE`,
`FOREIGN_OWNER`, `MALFORMED_PAYLOAD`, `DUPLICATE`, or `UNKNOWN`. It verifies
the current deterministic point identity and canonical SQLite owner/revision.
Payload alone is never deletion authority and `deletion_authorized` is always
false. A live scan or deletion requires a separate future authorization.

## Future production rollout (plan only)

1. Bind fresh canonical main, exact image/revision and current runtime mounts.
2. Stop writers under the production deployment authority.
3. Create and verify a fresh SQLite backup and rollback image artifact.
4. Require pre-migration `integrity_check=ok` and zero FK violations.
5. Bind the exact ordered expected/effective component scope and run only the
   canonical staged `memory_convergence` migration.
6. Require post-migration integrity/FK checks, expected tables/indexes,
   `vector_revision=1` defaults, and exactly one seed intent per legacy fact.
7. Keep vector mode off initially; restart and verify gateway/Telegram health.
8. Separately verify Qdrant health and collection contract.
9. Under a later activation authority, enable vector mode for bounded ticks;
   observe status until converged or a rollback trigger fires.
10. Never bundle historical orphan deletion with first rollout.

Rollback triggers include DB integrity/FK failure, unexpected seed count,
gateway health failure, owner/isolation evidence, BLOCKED work, stale backlog,
or provenance/config drift. Stop writers, restore the previous image, and
restore DB only when the additive migration itself damaged canonical SQLite.
Old code may leave the additive column/tables unused; queued intents remain for
a later forward restart. Historical Qdrant cleanup remains separate.
