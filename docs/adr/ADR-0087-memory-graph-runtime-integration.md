# ADR 0087: Memory Graph Runtime Integration

## Status
Accepted

## Context
The Memory Graph Engine components (PR-1 through PR-6) provide an adversarial-tested framework for managing derived graph insights. However, the runtime integration into the Hermes repository requires careful balancing between production stability and convergence logic. The graph extraction is currently unverified against real user queries.

## Decision
We introduce a bounded, disconnected graph runtime (MemoryGraphRuntime) running alongside the vector sync lifecycle.
1. **Dormancy by Default**: The graph engine will be disabled by default (MEMORY_GRAPH_MODE=disabled) and will not run out of the box.
2. **Shadow Mode**: We introduce MEMORY_GRAPH_MODE=shadow. When activated, it will:
    - Attempt to resolve queries via esolve_graph_context during request time.
    - NEVER merge results into production response pathways.
    - Emit metrics and queue background conversions upon cache misses/staleness.
3. **Bounded Background Sync**: A deduplicated queue with a maximum capacity of 128 items manages convergence. A background worker loop processes it one item at a time over a separate SQLite connection to preserve caller transaction isolation.
4. **Schema Safety**: We deliberately exclude graph schema auto-creation and migrations from runtime startup to avoid accidental persistence layer failures on the canonical application database.

## Consequences
- A separate connection must be opened by the background worker.
- The HealBiteMemoryBridge is modified to accept a graph_runtime property and issue shadow queries.
- We must run explicit staging migrations to enable the shadow mode safely in production.
