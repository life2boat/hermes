# ADR 0084: Deterministic Graph Read Path & Authoritative Context Hydration

**Date**: 2026-08-18  
**Status**: Accepted  
**Context**: Memory & Graph Engineering v3 (PR-4 foundation and PR-4.1 integrity closure)

## Context

With the persistent derived graph store established in PR-3, Hermes requires a mechanism to read semantic facts from the graph to provide context to LLM runs. However, the graph itself is a derived, rebuildable projection of the authoritative SQLite memory facts. It may be stale, corrupted, or missing. Furthermore, the graph layer structurally drops the exact values of `PROHIBITED_FIELD` and oversized properties to maintain strict privacy and safety boundaries. 

We must design a read path that leverages the structural advantages of the graph without compromising the security, freshness, and strict privacy guarantees of the authoritative source.

PR #199 provided the read-path foundation at head
`6a322c8ad2170b4e5a659fc6de2c7966ef4878d8` and squash merge
`2ab4daed932e0f7b4b088afcfc4e79f635fa830e`. Its CI sequencing passed. An
independent exact-main audit subsequently found three closure defects: numeric
SQLite order was positionally compared with lexical canonical fact-ID order;
placeholder tests overstated adversarial evidence; and the claimed pure Layer A
remained mixed into the database coordinator. PR-4.1 closes those defects
forward without activating the runtime path.

## Decision

We have implemented a two-layer deterministic graph read path (`gateway/memory/graph_query.py`):

1. **Layer A: Pure Graph Query (The Index)**
   - `query_graph_projection(projection, query)` operates only on a verified
     `GraphProjectionResult`; it accepts no connection and performs no I/O.
   - Returns structural `GraphStructuralMatch` values, never authoritative
     Memory OS rows.
   - Validates the exact `memory:user` -> `memory:has_entity` ->
     `memory:entity` -> `memory:has_fact` -> `memory:fact` topology and all
     required properties. Malformed or disconnected structure is a hard
     `GraphReadIntegrityError`; it is never silently skipped.
   - Must NOT attempt to resolve or inspect excluded properties or access secret values.
   - Enforces deterministic output ordering based on `(entity, key, value, node_id)`.

2. **Layer B: Authoritative Read Coordinator (The Hydrator)**
   - Serves as the sole entry point and orchestrates the read flow.
   - Canonicalizes current SQLite rows through `AuthoritativeSourceSnapshot`
     and compares complete canonical source-state equality with the persisted
     projection source. Missing or incomplete persisted source state is a hard
     integrity failure; unequal complete states return `STALE_GRAPH` with an
     empty context and never trigger a rebuild.
   - The full-source comparison is `O(n)` in authoritative fact count, bounded
     by existing Memory graph fact limits. It is not an `O(1)` check.
   - Uses canonical string fact IDs to resolve provenance directly, without an
     unguarded integer conversion.
   - Performs Authoritative Context Hydration: reconstructing the exact fact value from the verified authoritative source row.
   - Fail-closed: Any mismatch in revision, user ID, missing support row, or semantic content results in a hard `GraphReadIntegrityError`.

## Consequences

### Strict Isolation & Privacy Guarantees
Because Layer A (Graph Query) never receives or parses the excluded private values, it cannot accidentally leak them or use them for similarity operations. Excluded facts are completely dropped from the structure and will not match any query.

### Canonical Full-Source Freshness
Rather than rebuilding the graph on every read, the coordinator constructs the
same canonical source-state contract on both sides and compares them for exact
equality. The operation is `O(n)`. Fact insertion/read order, including the
numeric 9/10 boundary and sparse IDs, cannot create false staleness. A real add,
delete, revision change, count mismatch, or state mismatch returns
`STALE_GRAPH` with no context payload.

### Total Database Safety
The entire read path requires zero database modifications. By design, `conn.total_changes` remains unchanged, and no migration, schema inference, or write transactions are triggered. This guarantees the read path cannot accidentally corrupt the persistent graph state.

### Hydration Integrity
Hydration requires every canonical support row to exist and match revision,
requested user, entity, key, and value. Duplicate support evidence is
deduplicated and multi-support context is canonically ordered. Same-revision
direct authoritative semantic tampering violates the Memory update contract,
but a selected support with mismatched semantic content still fails closed.

This decision does not activate graph queries in gateway startup, handlers,
workers, prompt construction, or production runtime.
