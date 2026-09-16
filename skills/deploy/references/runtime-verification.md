# Runtime verification

Before mutation, collect a sanitized baseline: service identity and restart
count, exact DB mount, SQLite integrity/FK state, Qdrant health, feature and
protected-secret fingerprints, and rollback image readiness.

After an authorized exact-image deployment, require stable samples, exact
running image/OCI revision, zero unauthorized DB delta, SQLite integrity,
Telegram no-send health, security/isolation checks, and Qdrant
non-interference. Recreate only `hermes-bot`; Qdrant is not a deploy dependency.

Evidence failures after mutation are unverified deployment failures and use the
canonical rollback path. Keep evidence sanitized: no raw logs, dotenv content,
database rows, Telegram identities, or Qdrant payloads.
