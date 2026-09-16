# Qdrant rebuild boundary

Default to read-only metadata and `--dry-run`. Permit scoped upserts only with
explicit authority. Replacement, cutover, deletion, or collection cleanup needs
its own reviewed workflow and rollback point; retain proof that no delete or
collection switch occurred unless separately authorized.

The rebuild is upsert-only: successful current-fact upserts do not remove or
prove removal of stale Qdrant-only points. Pin the resolved URL, collection,
vector size, DB path, and optional user scope. Keep logs aggregate-only.

Fresh collection creation must reject existing names. Partial rebuild failure
forbids cutover; retain the old collection/configuration until later approved
cleanup.
