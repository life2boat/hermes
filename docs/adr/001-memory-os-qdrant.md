# ADR-001: Memory OS Semantic Search with Qdrant

## Problem

Hermes needs to retrieve a user's durable facts when the current wording differs
from the wording used when the fact was stored. SQLite FTS5 is reliable for
literal terms and prefixes, but lexical matching alone does not provide
meaning-based recall for paraphrases and related concepts.

## Context

Memory OS v2 introduced the Qdrant index in commit
`40bd8146939493c73c0eecc8d2a6c816ac435454`. The repository still treats
SQLite as the durable record: `memory_os_facts` stores the normalized facts,
while FTS5 and `LIKE` remain local fallback paths. Qdrant is optional and is
disabled unless its feature configuration is ready.

Qdrant hits are not returned as authoritative facts. The runtime filters the
vector query by `user_id`, then rehydrates each candidate from SQLite under the
same user predicate. This keeps semantic ranking separate from durable state
and authorization.

## Decision

Use Qdrant as an optional, derived semantic index over SQLite-backed Memory OS
facts.

- SQLite remains the durable source of truth.
- FTS5 remains the deterministic lexical fallback; `LIKE` is the final local
  fallback.
- Qdrant adds semantic recall when configured and healthy.
- Every vector query is scoped by normalized `user_id`.
- Every Qdrant result is revalidated and rehydrated from SQLite before use.
- Qdrant unavailability degrades recall quality; it must not make durable facts
  unavailable or mutate SQLite.

## Alternatives Considered

- **SQLite FTS5 only.** Operationally simple and useful for literal search, but
  insufficient alone for semantic similarity and paraphrase recall.
- **Qdrant as the primary database.** Rejected because vector storage is an
  index optimized for retrieval, not the authoritative transactional record.
- **Replace FTS5 with Qdrant.** Rejected because it would remove a local,
  deterministic fallback and make memory retrieval depend on another service.
- **Use an external managed memory service.** Rejected at this stage because it
  adds another authority boundary without removing the need for local durable
  state and tenant isolation.

## Consequences (+ and -)

**Positive**

- Users can retrieve relevant facts even when query wording changes.
- SQLite remains sufficient for correctness and degraded operation.
- Rehydration prevents stale vector payloads from becoming authoritative.
- The design permits rebuilding Qdrant from durable SQLite state.

**Negative**

- Two stores introduce drift and reconciliation work.
- Semantic retrieval adds service, embedding, latency, and observability
  dependencies.
- A healthy Qdrant service does not prove that its contents converge with
  SQLite; explicit reconciliation evidence is required.
