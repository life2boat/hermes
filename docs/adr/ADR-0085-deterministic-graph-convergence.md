# ADR 0085: Deterministic Graph Convergence & Orchestration

**Date**: 2026-08-19  
**Status**: Accepted  
**Context**: Memory & Graph Engineering v3 (PR-5)  

## Context

Following PR-4's implementation of the exact deterministic read path, we need an orchestrated way to rebuild the persistent derived graph when it becomes stale, without mutating database connection state arbitrarily and without causing un-bounded retry loops under high concurrency. The graph acts as an index of the SQLite Memory OS facts. If facts change, the graph goes `STALE` and must be brought back to `CURRENT`. 

## Decision

We have implemented a module `gateway/memory/graph_convergence.py` serving as the definitive public entrypoint for graph convergence: `converge_user_graph(conn, user_id) -> GraphConvergenceResult`.

This process follows explicit guarantees:
1. **Idempotency**: If the graph is `CURRENT`, it executes a strict `NOOP` and returns immediately.
2. **Missing and Stale Rebuild**: If the graph is `MISSING` or `STALE`, it drives the full `rebuild_user_graph_store` pipeline.
3. **Empty and Excluded Scopes**: Convergence successfully handles zero-fact states and completely excluded states by treating them appropriately (the graph normalizes to zero nodes, which is a valid `CURRENT` state).
4. **Transaction Ownership**: Convergence executes synchronously using the caller-owned SQLite connection. It does not spawn background tasks or manage connection lifecycles.
5. **Bounded Retry**: The convergence loop runs a maximum of 3 times. If concurrent writes (or other transactional anomalies) cause the graph to be instantly `STALE` after publishing, it retries. If churn exhausts the boundary, it throws a `GraphConvergenceError` for `SOURCE_CHURN_EXHAUSTION`.
6. **Integrity Hard Fail**: Any detected corruption in the existing store instantly aborts convergence with a hard fail. There is zero `AUTO_OVERWRITE_CORRUPTION`. Corrupted graphs must be addressed by operational interventions, not masked by automated overwrites.

## Consequences

- The `read_graph_context` from PR-4 remains purely read-only and explicitly `READ_PATH_AUTO_REBUILD=false`. Convergence is an orchestrated, independent operation.
- Calling `converge_user_graph` twice consecutively results in exactly zero database mutations on the second call.
- Any secret `PROHIBITED_FIELD` records are structurally excluded during projection, and the orchestrator treats this naturally without leaking values to the final nodes table.
- Strict isolation guarantees ensure converging one user does not affect or hydrate the graph of another.
