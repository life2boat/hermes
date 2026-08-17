# ADR-0081: Memory Graph Engineering v3 Architecture

## Problem

The current Hermes memory architecture relies on authoritative SQLite (`memory_os_facts`) for durability and a Qdrant vector index for semantic retrieval. As Hermes evolves to require complex relation traversal, we need a deterministic graph layer without breaking existing invariants or trusting LLM-generated assertions that bypass the authoritative store.

## Context

Current authoritative boundary:
- **SQLite**: Authoritative source of facts.
- **Qdrant**: Derived, rebuildable, untrusted semantic acceleration state.

LLMs lack the reliability to act as the authoritative source of truth for graph relations. Allowing an LLM to directly assert a relation into a trusted graph database would create untraceable, potentially malformed or conflicting edges that cannot be deterministically invalidated if the underlying source fact is retracted.

## Decision

We will introduce a Graph Memory Layer (v3) built upon strict deterministic contracts.

1. **Authority**:
   - SQLite `memory_os_facts` remains AUTHORITATIVE.
   - The graph layer is strictly DERIVED_REBUILDABLE.
   - No LLM-proposed relation is trusted until explicitly bound to an authoritative SQLite source fact via `GraphProvenance`.

2. **Graph Contracts**:
   - We define `GraphNode` and `GraphEdge` with a versioned schema (`GRAPH_SCHEMA_VERSION = 1`).
   - Each entity possesses a **deterministic, content-bound identity** computed from its type, canonical properties (sorted JSON), and provenance.
   - Identity guarantees that identical inputs always yield the same hash ID, implicitly handling deduplication and idempotency.

3. **Provenance**:
   - Every graph assertion is bound via `GraphProvenance` (e.g., `source_system`, `fact_id`, `revision`).
   - If an authoritative fact is deleted or superseded (revision bumped), all derived graph entities tied to that exact `fact_id` and `revision` are deterministically orphaned and can be safely purged.

4. **Validation and Security**:
   - Tamper detection: Entities can re-compute their IDs and fail closed if mutated.
   - Secret-like material (e.g., tokens, API keys) in graph properties is explicitly rejected to prevent leakage into the derived search space.
   - Self-edges are explicitly forbidden.

5. **Rebuildability**:
   - The entire graph state can be dropped and deterministically re-projected from the SQLite facts.
   - A reconciliation loop (similar to the Qdrant outbox) will eventually manage the projection of SQLite facts into the derived graph store.

## Consequences

**Positive**
- Total provenance: We can answer exactly why an edge exists and what source fact justifies it.
- Safe rollbacks: The graph can be destroyed and rebuilt without data loss.
- Protection against LLM hallucinations mutating the core relationship store.

**Negative**
- Increased architectural complexity due to the separation of extraction (LLM) and projection (Engine).
- Write amplification: A single fact might project to multiple nodes and edges.
- Latency between fact storage and graph availability during asynchronous projection.

## Roadmap (v3 Sequence)

Based on the repository analysis, the following sequence will execute the Graph Engineering v3 transition:

- **PR-1**: Architecture + deterministic graph contracts (This ADR).
- **PR-2**: Authoritative-source → graph projection engine.
- **PR-3**: Persistent derived graph storage / schema (SQLite/Neo4j).
- **PR-4**: Graph retrieval and hybrid vector+graph query layer.
- **PR-5**: Convergence / reconciliation / invalidation for graph derived states.
- **PR-6**: Behaviour evals + quality corpus for graph traversals.
- **PR-7**: Release gates + observability.
- **PR-8**: Staged production migration / activation.
