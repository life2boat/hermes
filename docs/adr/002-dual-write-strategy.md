# ADR-002: SQLite-First Dual-Write Strategy

> The authority direction in this ADR remains valid. Its best-effort delivery
> mechanism is superseded by ADR-0079's durable transactional outbox.

## Problem

Memory facts must be durable in SQLite and discoverable through Qdrant, but the
two systems do not share a transaction. A partial failure must not lose the
authoritative fact, expose another user's state, or be mistaken for successful
index convergence.

## Context

The Memory OS runtime commits a fact to SQLite before scheduling the Qdrant
upsert. The Qdrant adapter is best-effort, timeout-bounded, and allowed to fail
without rolling back SQLite. Rebuild and reconciliation tooling can replay
SQLite facts into the vector index.

This ordering creates an intentional recovery direction: SQLite can recreate
Qdrant, while Qdrant must never overwrite SQLite merely because a vector point
exists. Equal row and point counts are not sufficient convergence evidence;
identities, ownership, payloads, and mutation coverage also matter. In
particular, an upsert-only reconciliation does not prove that stale deleted
points are absent.

## Decision

Use a SQLite-first authority direction. ADR-0079 refines delivery: the fact
mutation and minimal revisioned vector intent commit atomically in SQLite, then
a bounded worker performs owner-scoped idempotent upsert/delete reconciliation.
Qdrant failure remains index lag rather than failure of the canonical write.

## Alternatives Considered

- **Distributed two-phase commit.** Rejected because SQLite and Qdrant do not
  provide a practical shared transaction, and the operational complexity would
  exceed the value of the derived index.
- **Qdrant-first writes.** Rejected because a later SQLite failure would leave a
  discoverable point without an authoritative fact.
- **Synchronous all-or-nothing application behavior.** Rejected because a
  transient Qdrant outage would incorrectly make durable memory writes
  unavailable.
- **Qdrant as reconciliation authority.** Rejected because index contents can
  be stale and are not the durable record.

## Consequences (+ and -)

**Positive**

- Durable writes remain available during Qdrant failures.
- Recovery has one clear direction: SQLite to Qdrant.
- Index mutations can be retried idempotently with deterministic identities.
- Tenant checks are applied at both the durable and derived boundaries.

**Negative**

- The vector index can lag or retain stale points.
- Operators need reconciliation, drift metrics, and deletion-aware procedures.
- A successful write response proves SQLite durability, not immediate semantic
  index availability.
- Reconciliation must validate identities and payload ownership, not only
  counts.
