# Memory authority and isolation

Scope every Memory OS fact read and write by normalized `user_id`. When
household-owned data is involved, resolve permission through the authoritative
household authorization context; raw transport or domain IDs do not prove access.
Require Qdrant `user_id` filters/payloads and SQLite re-hydration of semantic
hits, with synthetic cross-user/cross-household denial tests.

Keep SQLite as the durable source of truth. Treat Qdrant and SQLite FTS as derived
search indexes; derived drift must not overwrite durable facts. During a
Qdrant-only operation, SQLite schema/content fingerprints remain unchanged.

Resolve live paths from runtime state. Before SQLite mutation, require a
verified backup plus pre/post `PRAGMA integrity_check`, `PRAGMA foreign_key_check`,
and a successful isolated restore test.
