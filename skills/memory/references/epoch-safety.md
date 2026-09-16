# Fact UUID and epoch safety

New `memory_os_facts` use immutable `fact_uuid`; semantic updates retain the
UUID and advance `vector_revision`. Derived point ID is
`uuid5(NAMESPACE_URL, f"healbite-memory:{user_id}:{fact_uuid}")`; the SQLite
integer ID is a lookup hint, not a global identity.

Legacy backfill uses one externally assigned `legacy_epoch_uuid`, pinned through
authority approval, plan, and final execution authority. Backfill identity is
`uuid5(NAMESPACE_URL, f"healbite-fact-legacy:{legacy_epoch_uuid}:{user_id}:{sqlite_id}")`.
Independent writable legacy histories require different epochs. An authority/DB
epoch mismatch fails before writes; UUID-native fresh DBs retain the NULL marker.

`memory_convergence_v2` preserves fact/outbox identities and never authorizes
SQLite migration or Qdrant cutover by itself. Hydration requires exact UUID,
revision, and owner equality against SQLite.
