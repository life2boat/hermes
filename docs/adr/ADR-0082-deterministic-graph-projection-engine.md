# ADR 0082: Deterministic Graph Projection Engine (PR-2)

## Status
Accepted

## Context
In PR-1 (ADR 0081), we established a deterministic Memory Graph Engineering v3 contract `GraphSnapshot` which dictates tamper-proof identity, bounded structural semantics, and adversarial duplicate-key parser protections.

However, we need an engine to deterministically project the `memory_os_facts` authoritative SQLite baseline into these snapshot contracts. The engine must adhere to the two-layer design:
- Layer A: `AuthoritativeMemoryFact` mapping straight from SQLite (pure data struct).
- Layer B: Pure Projection engine generating a `GraphProjectionResult`.

The projection must also adhere to bounds checking (max facts), privacy exclusion constraints (secret filtering), and strict deterministic topological layout mapping for entity and fact nodes.

## Decision
We implemented a pure deterministic graph projection engine in `gateway/memory/graph_projection.py`.
1. **Layer A Adapter**: `read_authoritative_memory_facts` issues an `ORDER BY id ASC` query to read `AuthoritativeMemoryFact` instances, maintaining deterministic layout without any provider/LLM involvement.
2. **Layer B Engine**: `project_authoritative_memory_facts` iterates exactly once over the bound input structure.
3. **Graph Morphology**: For a single user, we project:
   - 1 `memory:user` node
   - N `memory:entity` nodes
   - M `memory:fact` nodes
   - Edge relationships linking `memory:user` -> `memory:has_entity` -> `memory:entity` -> `memory:has_fact` -> `memory:fact`.
4. **Privacy & Exclusions**:
   - Any property violating key or string value length bounds (100 and 4096 bytes respectively) is excluded.
   - Any key bearing secret semantics ("password", "credential", etc.) is excluded.
   - Exclusions are deterministically bound to the final `projection_id`.
5. **Limits Check**: If facts > 499 (which prevents structural size explosion beyond `MAX_GRAPH_NODES`), we fail closed with `PROJECTION_LIMIT_EXCEEDED` and expect future PRs to define explicit sharding.

## Consequences
- We successfully decouple authoritative storage from semantic retrieval boundaries.
- Deterministic graph snapshots are strictly bound to their canonical facts through cryptographic identity (the `projection_id` digests all components and exclusion states).
- No LLMs are allowed in this structural boundary; the graph strictly mirrors facts.
