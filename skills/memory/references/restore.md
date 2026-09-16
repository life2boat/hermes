# SQLite restore and derived-index recovery

After any SQLite restore, rollback, or replacement, disable conversational
memory serving under explicit authority and distrust the old vector index.
UUID hydration blocks stale recall but does not clean discarded-timeline points.

Authorized recovery is: quiesce writers/reconciler → verified SQLite restore →
canonical staged v2 migration when still legacy → fresh collection rebuild from
the restored authoritative scope → verify acknowledgements, identity/revision
coverage, isolation, and outbox convergence → separately authorize cutover and
serving requalification.

Do not restore SQLite for a Qdrant-only failure. Do not delete the old collection
or enable vector serving as part of diagnosis.
