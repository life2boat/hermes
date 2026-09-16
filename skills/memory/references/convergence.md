# Durable convergence

Commit each applicable Memory OS fact mutation and its minimal revisioned
`UPSERT`/`DELETE` intent in one SQLite transaction. The durable outbox, not an
in-memory future, is the restart recovery source; Qdrant remains asynchronous
derived state.

Classify safe aggregate outbox state first. `PENDING`, `DEGRADED`, and `BLOCKED`
are not converged. Equal global counts do not prove identity equality or the
absence of historical orphan points. Prefer an authorized bounded tick for
known durable intents over a broad rebuild.

Hydration must re-check owner, current revision, and identity against SQLite.
Missing or stale derived payloads fail closed.
