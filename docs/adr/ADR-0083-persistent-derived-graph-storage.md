# ADR 0083: Persistent Derived Graph Storage (PR-3)

## Status
Accepted

## Context
Following PR-1 and PR-2, which established deterministic graph snapshots and the memory graph projection engine, we now need a mechanism to persist these derived graphs. The SQLite `memory_os_facts` table remains the sole authoritative source of truth. The stored graph must be strictly derived, disposable, and offline-rebuildable. It is critical that saving this graph into the database does not blur the lines of authority. Furthermore, we need guarantees of atomicity, cryptographic tamper detection upon reading, and strong user isolation.

## Decision
We implemented a Persistent Derived Graph Storage engine in `gateway/memory/graph_store.py`.

1. **Schema Authority & Scope**:
   - `MEMORY_GRAPH_STORE_SCHEMA_VERSION = 1`.
   - The graph store runs in the same SQLite database as `memory_os_facts`, using distinct, derived tables.
   - Authoritative tables (`memory_os_facts`, `memory_os_vector_sync_outbox`, `memory_os_vector_sync_meta`) remain completely untouched. No graph inputs or updates can ever expand or modify authoritative facts.

2. **Normalized User-Scoped Schema**:
   - Tables: `memory_graph_store_meta`, `memory_graph_user_state`, `memory_graph_nodes`, `memory_graph_edges`, `memory_graph_node_supports`, `memory_graph_edge_supports`, `memory_graph_exclusions`.
   - All child rows enforce strict foreign keys to their parent `user_id` and structural parents (e.g. edge supports -> edges).
   - Multi-support provenance is persisted exactly as it is projected.

3. **Atomic Publish Semantics**:
   - Publishing an updated graph strictly replaces the existing user graph within a single `SAVEPOINT` transaction boundary.
   - If the write fails, it is rolled back completely. The previous projection remains intact.
   - Rebuilding a graph drops and repopulates the derived tables deterministically.

4. **Cryptographic Read-Back Integrity (Fail Closed)**:
   - When loading, we do not blindly trust the tables. The graph snapshot is deserialized from `canonical_snapshot_json`, verified against `db_snapshot_id`, and its topological contents (node/edge properties, primary provenances, counts) are strictly cross-checked against the row-level data in the nodes and edges tables.
   - Any corruption (e.g. a deleted node row, tampered property) throws `GraphStoreError`.
   - Stale writes or unexpected concurrency are implicitly rejected by the exact projection ID re-verification logic and by offline isolation.

5. **No Production Mutation**:
   - This capability operates offline.
   - We did not activate graph storage in the `MemoryVectorRuntime`, `gateway`, Telegram loop, or any production endpoint.
   - It is strictly uncoupled from Qdrant.

## Consequences
- We now possess the capability to cache derived semantic graphs, saving expensive projection compute.
- The `GraphSnapshot` serialization is fully deterministic, ensuring stable snapshot IDs.
- Graph retrieval (PR-4) can proceed on top of this safely-isolated read model without risking corruption of authoritative memory.
