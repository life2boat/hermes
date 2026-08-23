# ADR-0088: Memory Graph Shadow Activation Readiness

## Context
Hermes has introduced graph-based memory convergence in PR-7. We are moving towards activating this new infrastructure in a controlled, fail-safe manner. Due to the critical nature of the authoritative memory facts and SQLite data, a staged approach is strictly required.

## Decision
We will execute the Memory Graph activation in two distinct phases:
1. **Shadow Activation (Phase 1):** The memory graph runs in the background. It receives updates and tests infrastructure stability but its query results are *not* served to the LLM context (`GRAPH_CONTEXT_SERVED_TO_USERS=false`).
2. **Serve Activation (Phase 2):** After gathering sufficient shadow telemetry (the "shadow health receipt"), we will evaluate activating the graph context serving. This ADR forbids Serve Mode for Phase 1.

### Principles
- **SQLite Remains Authoritative**: All canonical facts are persisted in SQLite `memory_os_facts`.
- **Graph Schema is Derived**: The graph indices are rebuilt or derived from SQLite.
- **Fail-Closed on Incompatible Schema**: If the graph schema is incompatible, migration and activation fail closed instead of auto-repairing or dropping data.
- **Exact-Image Provenance**: Migration and activation run against an exact `origin/main` commit and specific container image.
- **Staged Migration**: Production SQLite is never migrated in place. A private copy is created, migrated, validated, and finally published atomically.
- **Fresh Backup Requirement**: A fresh, verified backup is required before staging.
- **Rollback Unit**: Any rollback requires reverting *both* the database and the exact container image.
- **Operator Authorization**: Preflight technical readiness never auto-approves execution. A human operator must explicitly authorize production mutation.

## Consequences
- Activation requires zero writers/controlled quiescence during the atomic publish.
- Serve mode remains hard-disabled in production settings until explicitly allowed.
- Future runbooks rely on deterministic receipt generation (Shadow Health Receipt) for canary health validation.
