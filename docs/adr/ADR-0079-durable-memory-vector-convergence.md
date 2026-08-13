# ADR-0079: Durable SQLite-to-Qdrant Memory Convergence

## Problem

HealBite Memory OS stores authoritative facts in SQLite and projects them into
Qdrant for semantic recall. The previous SQLite-first asynchronous upsert left
two correctness gaps: a process could commit SQLite and exit before any durable
record of the required vector mutation existed, and deleting a SQLite fact did
not create a Qdrant delete. Hydration prevented a stale point from becoming
authoritative, but did not remove the stale derived data or expose durable lag.

## Context

SQLite and Qdrant cannot share a transaction. Making Qdrant availability a
condition of committing a canonical fact would invert the authority model and
make durable memory unavailable during an index outage. An in-memory Future is
useful for latency but cannot be the recovery source after restart.

The point identity is already deterministic from `(user_id, SQLite fact id)`.
SQLite fact ids use `AUTOINCREMENT`, so a delete/recreate receives a new point
identity. A monotonically increasing `vector_revision` orders updates to one
fact identity. Together they provide generation and revision semantics without
using timestamps as the ordering authority.

## Decision

Use a durable transactional outbox in the same SQLite database as the
authoritative facts.

1. A fact insert/update/delete and its minimal `UPSERT` or `DELETE` intent
   commit in one SQLite transaction.
2. The outbox stores identity, owner, operation, revision, state, attempt count,
   retry time and a closed safe error class. It stores no fact text, embedding,
   prompt, provider response or secret.
3. A bounded reconciliation tick re-reads current canonical fact content,
   validates owner and revision, and performs only the scoped deterministic
   point mutation.
4. Reconciliation requests Qdrant `wait=true`. Request acceptance without a
   strong acknowledgement remains unresolved work.
5. Successful operations are removed from the outbox and reflected in a
   singleton aggregate status row. Failed work remains `RETRY` with bounded
   exponential backoff or becomes `BLOCKED` at the attempt limit.
6. A crash after the Qdrant mutation but before SQLite acknowledgement repeats
   the same deterministic operation. Both upsert and delete are idempotent.
7. An old upsert is superseded when SQLite contains a newer revision. An old
   delete is superseded when its fact identity currently exists. A missing fact
   reached by a stale upsert is converted into the same scoped delete, never a
   resurrection.
8. Mutations while vector mode is disabled still enqueue intent. They remain
   `PENDING` without contacting Qdrant and can converge after an authorized
   re-enable.
9. SQLite rehydration of Qdrant hits remains mandatory during any backlog.

## Delivery and Failure Semantics

Delivery is at least once. `CONVERGED` means no unresolved outbox rows;
`PENDING` means durable unattempted work; `DEGRADED` means retryable work;
`BLOCKED` means terminally unresolved or malformed work. A canonical SQLite
mutation remains successful when Qdrant fails, but the derived-state health
must not be reported as converged.

The reconciliation worker is bounded by batch size and wall-clock budget. It
has no recursive retry or sleep loop. A Qdrant client initialization failure can
be retried on a later due tick. One operation failure does not prevent later
operations in the same batch from being examined.

The normal gateway owns one reconciliation task when vector mode is enabled.
It performs a bounded startup tick and bounded periodic ticks, publishes
privacy-safe health through the existing runtime-status model, and stops with a
bounded wait. Durable intent remains the shutdown/restart guarantee. Vector-off
startup does not create/open the canonical database or contact Qdrant.

Terminal repair is not a global queue reset. It requires an explicit bounded
set of internal operation ids and one owner scope, then revalidates canonical
owner, existence and revision before applying the ordinary idempotent operation.

## Migration and Rollback

`gateway/memory/schema.py` is the single low-level schema authority for
`memory_os_facts` convergence state. The ordered production migration registry
owns a `memory_convergence` component that adds `vector_revision`, creates the
outbox/meta tables and indexes, and seeds exactly one pending upsert for each
legacy fact. The seed and its completion marker share the staged migration's
SQLite transaction. Safe development/test initialization reuses the same
contract; production runtime startup validates it read-only and cannot replace
the staged migration. Re-running either path does not duplicate intents.

There is no destructive downgrade migration. Code rollback may leave the new
tables/column unused; SQLite readers tolerate additive schema. Before any future
production rollout, the ordinary database backup/migration/rollback contracts
still apply. This ADR and its repository tests do not authorize production
migration or Qdrant mutation.

## Security and Isolation

Every operation carries the normalized owner used by the canonical mutation.
The worker revalidates the current row owner before upsert, derives point ids
from owner plus fact id, and never accepts a caller-supplied global point id.
Malformed and cross-owner operations become `BLOCKED` without a Qdrant call.
Observability contains counts, age, timestamps and closed error classes only.

## Alternatives Considered

- **Keep best-effort Futures plus periodic rebuild.** Rejected because the
  crash window and deletes have no durable recovery intent.
- **Make Qdrant synchronous and transactional from the caller's perspective.**
  Rejected because Qdrant is derived and must not make SQLite unavailable.
- **Full collection scans on every write.** Rejected as unbounded, destructive
  and unnecessary when known intents are available.
- **Store full fact payloads in the outbox.** Rejected as redundant private
  data; current content is safely re-read from SQLite for an upsert.
- **Order solely by timestamps.** Rejected in favor of deterministic fact
  generations and integer revisions.

## Consequences (+ and -)

**Positive**

- A committed applicable fact mutation always has restart-safe derived intent.
- Deletes converge without making Qdrant authoritative.
- Duplicate delivery and acknowledgement crashes are safe.
- Backlog health is queryable without exposing memory content.
- Disabled vector mode no longer silently loses future convergence work.

**Negative**

- SQLite gains additive schema and a small amount of write amplification.
- Semantic recall may lag while status is pending or degraded.
- Terminally blocked operations require an explicitly authorized repair action.
- Full historical orphan discovery remains a separate, explicitly authorized
  collection-reconciliation scope; this design processes known durable intents.
- Repository v1.1 provides an offline classifier but no deletion API or
  live-scan authorization.
