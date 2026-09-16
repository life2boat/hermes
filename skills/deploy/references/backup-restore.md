# Backup and restore boundary

Before SQLite mutation, create a fresh backup from the exact live database via
the SQLite backup API or approved online equivalent. An older backup does not
represent the state immediately before an authorized mutation.

Record timestamp and SHA-256, verify integrity and foreign keys, restore to an
isolated path, and require a successful isolated restore before DDL. Do not use
a plain copy of an active database.

Restore only for confirmed corruption with explicit operator authorization and
an accepted data-loss window. Stop writers, preserve the failed DB, atomically
restore the verified backup, then verify integrity and foreign keys before the
approved runtime starts.
