# Live database safety

Resolve the exact live SQLite path from runtime mounts; a guessed path can
create or validate the wrong database. Before state capture or migration
publication, require active writers to be zero.

Require pre/post `PRAGMA integrity_check` and `PRAGMA foreign_key_check`, plus
schema, user-version, and structural fingerprints. A healthy container cannot
compensate for a corrupt database or a schema-breaking migration whose rollback
image is incompatible.

Require compatibility tests for the candidate and rollback images before an
authorized schema-breaking release.

Record only path/device/inode, hashes, classifications, and aggregate counts.
Never print rows, identifiers, secrets, or protected configuration.
