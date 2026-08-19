# ADR 0085: Deterministic Graph Convergence & Orchestration

**Date**: 2026-08-19
**Status**: Accepted
**Context**: Memory & Graph Engineering v3 (PR-5)

## Context

Following PR-4's implementation of the exact deterministic read path, we need an orchestrated way to rebuild the persistent derived graph when it becomes stale, without mutating database connection state arbitrarily and without causing un-bounded retry loops under high concurrency. The graph acts as an index of the SQLite Memory OS facts. If facts change, the graph goes `STALE` and must be brought back to `CURRENT`.

## Decision

We have implemented a module `gateway/memory/graph_convergence.py` serving as the definitive public entrypoint for graph convergence.

### API Contract

```python
inspect_graph_convergence(conn, *, user_id) -> GraphConvergenceAssessment
converge_user_graph(conn, *, user_id, max_attempts=3) -> GraphConvergenceResult
```

### States and Statuses

`GraphConvergenceState`:
- `CURRENT`
- `MISSING`
- `STALE`

`GraphConvergenceStatus`:
- `NOOP_CURRENT`
- `REBUILT_MISSING`
- `REBUILT_STALE`
- `SOURCE_CHURN_RETRY_EXHAUSTED`

### Guarantees

1. **Idempotency**: If the graph is `CURRENT`, it executes a strict `NOOP` and returns immediately (`NOOP_CURRENT`).
2. **Missing and Stale Rebuild**: If the graph is `MISSING` or `STALE`, it drives the rebuilding pipeline.
3. **Empty and Excluded Scopes**: Convergence successfully handles zero-fact states and completely excluded states by treating them appropriately.
4. **Transaction Ownership**: Convergence executes synchronously using the caller-owned SQLite connection. It relies strictly on caller transaction ownership. It does not spawn background tasks, `conn.commit()`, or `conn.rollback()`.
5. **Bounded Retry**: The convergence loop runs `1..5` attempts. 
   - Uses a **pre-publish projection barrier** to detect race mutations before writing.
   - Uses a **post-publish verification** to ensure exact data persisted.
   - If churn exhausts the boundary, it returns a deterministic result status (`SOURCE_CHURN_RETRY_EXHAUSTED`). It does not raise exceptions for churn.
6. **Integrity Hard Fail**: Any detected corruption in the existing store instantly aborts convergence with a hard fail (`GraphConvergenceIntegrityError`). `AUTO_OVERWRITE_CORRUPTION=false`. Corrupted graphs must be addressed by operational interventions.
7. **Canonical Source Builder**: Convergence relies on unified `CANONICAL_SOURCE_STATE_BUILDERS=1` helper (`build_authoritative_source_snapshot`) to guarantee structural equivalence checking.
8. **Activation**: No runtime activation (`GRAPH_CONVERGENCE_RUNTIME_ACTIVATED=false`). 
9. **Read Path**: The read path remains purely read-only (`READ_PATH_AUTO_REBUILD=false`).

## Consequences

- Calling `converge_user_graph` twice consecutively results in exactly zero database mutations on the second call.
- Strict isolation guarantees ensure converging one user does not affect or hydrate the graph of another.
