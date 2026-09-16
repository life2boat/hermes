# Migration scope and rehearsal

`MIGRATION_COMPONENTS` remains the complete ordered canonical registry. Bind
operator authority to `EXPECTED_MUTATION_COMPONENTS`, derive live
`COMPONENT_SCHEMA_STATES` and `EFFECTIVE_MUTATION_COMPONENTS`, and require
exact expected/effective equality at planning and immediately before DDL.

Rehearse the full additive initializer on a production-derived staging copy;
verify schema/index fingerprints, `PRAGMA integrity_check`,
`PRAGMA foreign_key_check`, unrelated-table preservation, and idempotency.
Do not repair production with ad-hoc DDL.

`memory_convergence_v2` is additive and uses a pinned `LEGACY_EPOCH_UUID`
through approval, plan, and final authority. It does not authorize a production
migration, collection-pointer change, or rollout expansion.
