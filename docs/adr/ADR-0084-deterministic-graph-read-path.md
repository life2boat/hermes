# ADR 0084: Deterministic Graph Read Path & Authoritative Context Hydration

**Date**: 2026-08-18  
**Status**: Accepted  
**Context**: Memory & Graph Engineering v3 (PR-4)  

## Context

With the persistent derived graph store established in PR-3, Hermes requires a mechanism to read semantic facts from the graph to provide context to LLM runs. However, the graph itself is a derived, rebuildable projection of the authoritative SQLite memory facts. It may be stale, corrupted, or missing. Furthermore, the graph layer structurally drops the exact values of `PROHIBITED_FIELD` and oversized properties to maintain strict privacy and safety boundaries. 

We must design a read path that leverages the structural advantages of the graph without compromising the security, freshness, and strict privacy guarantees of the authoritative source.

## Decision

We have implemented a two-layer deterministic graph read path (`gateway/memory/graph_query.py`):

1. **Layer A: Pure Graph Query (The Index)**
   - Operates strictly on the canonical graph snapshot representation.
   - Responsible for matching structural relationships (`memory:entity` -> `memory:has_fact` -> `memory:fact`) and fast filtering by entity or key.
   - Must NOT attempt to resolve or inspect excluded properties or access secret values.
   - Enforces deterministic output ordering based on `(entity, key, value, node_id)`.

2. **Layer B: Authoritative Read Coordinator (The Hydrator)**
   - Serves as the sole entry point and orchestrates the read flow.
   - Executes an O(1) freshness check (fast equality check) against the current `memory_os_facts` by comparing the exact ordered sequence of `fact_id` and `current_revision`.
   - Uses the `node_provenance` (`GraphProvenance`) emitted by Layer A to lookup the exact `vector_revision`, `user_id`, and `sqlite_id` in the current authoritative facts.
   - Performs Authoritative Context Hydration: reconstructing the exact fact value from the verified authoritative source row.
   - Fail-closed: Any mismatch in revision, user ID, missing support row, or semantic content results in a hard `GraphReadIntegrityError`.

## Consequences

### Strict Isolation & Privacy Guarantees
Because Layer A (Graph Query) never receives or parses the excluded private values, it cannot accidentally leak them or use them for similarity operations. Excluded facts are completely dropped from the structure and will not match any query.

### Fast Equality Semantics for Freshness
Rather than rebuilding the graph on every read, the coordinator performs a fast sequence equality check against the authoritative source. If `len(current_facts) != len(snapshot_auth_facts)` or if any `(fact_id, revision, status)` tuple differs, the read path safely returns a `STALE_GRAPH` status with no context payload, avoiding potentially unbounded background rebuilds during a read operation.

### Total Database Safety
The entire read path requires zero database modifications. By design, `conn.total_changes` remains unchanged, and no migration, schema inference, or write transactions are triggered. This guarantees the read path cannot accidentally corrupt the persistent graph state.

### Hydration Integrity
Because hydration happens via direct SQLite row ID and vector revision comparison, any tampering with the snapshot's canonical representation that goes undetected by the fast hashing (due to corruption or adversarial injection) will immediately trigger a hard failure during the authoritative cross-check.
