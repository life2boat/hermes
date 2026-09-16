# Rollback and legacy bootstrap

Recreate only with the exact inspected immutable image without tag re-resolution.
After a schema-breaking migration, the previous image is not a valid automatic
recovery target until compatibility is proven.

Ordinary deploy requires a valid previous-image OCI revision. A missing legacy
revision uses only the separate one-time
`scripts/hermes_legacy_provenance_bootstrap.py` contract: plan-bootstrap →
rehearse-rollback → validate-bootstrap → authorized execute-bootstrap. It binds
root-private archive identity and may classify the source only as
`LEGACY_BASELINE` with `SOURCE_REVISION=UNKNOWN`.

Do not restore a pre-migration database merely to roll back an application
image. A healthy rollback is `ROLLED_BACK`, never `PASS`.
