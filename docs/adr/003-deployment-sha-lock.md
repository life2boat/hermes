# ADR-003: Exact-SHA and Immutable-Image Deployment Lock

## Problem

A production deployment must prove exactly which reviewed source and image are
running and must preserve a valid recovery state. Mutable tags, dirty build
contexts, implicit migrations, and incomplete rollback evidence make a
technically successful container start insufficient proof of a safe release.

## Context

The reproducible production deployment source of truth was introduced in
commit `d911291f42b8b110cd3ae255fcd7b5ce4c697f40` and subsequently hardened with
repository provenance, exact-main image builds, staged migration authorities,
and full image secret scanning. The deployment skill records the accumulated
safety contract.

GitHub CI proves repository checks for a commit, but it is not production
mutation authority. The production gate must independently bind the canonical
remote and main SHA, exact Git tree, immutable image identity, OCI revision,
live state, migration scope, and rollback candidate.

## Decision

Deploy only an exact, immutable image proven to correspond to the canonical
main SHA.

- Require a clean checkout whose `HEAD`, canonical remote main, and approved
  release SHA are identical; bind the exact Git tree used for the build.
- Build from a trusted context and reject secrets or unrelated worktree files.
- Address the candidate by immutable registry digest or inspected local image
  ID, and require its OCI revision to equal the exact source SHA.
- Require canonical full-image scanning of metadata, layers, and final
  filesystem, plus successful exact-head CI.
- Treat CI as necessary evidence, not as authorization to mutate production.
- Before state capture or migration, prove zero active writers, take a fresh
  SQLite backup through the backup API, and verify restore evidence, integrity,
  foreign keys, schema compatibility, and exact effective migration scope.
- Deploy the inspected immutable image, not a mutable tag.
- Permit automatic image rollback only when the previous image is locally
  available and compatible with the post-migration schema. Database restore is
  a separate, explicit authority.
- Keep governance warnings distinct from technical gates: warnings do not
  downgrade a technical PASS, while a failed or unknown technical gate cannot
  be bypassed.

## Alternatives Considered

- **Deploy a tag such as `latest` or `main`.** Rejected because the referenced
  bytes can change after approval.
- **Build from the operator's current checkout.** Rejected because untracked or
  modified files break reviewed-source provenance.
- **Treat green CI as deployment authority.** Rejected because CI does not prove
  the live database, writers, secrets, mounts, capacity, or rollback state.
- **Run migrations implicitly during container startup.** Rejected because it
  hides the writer boundary, effective changeset, and recovery point.
- **Always roll back only the image.** Rejected because a schema-breaking
  migration can make the old image incompatible.

## Consequences (+ and -)

**Positive**

- Production identity is reproducible from source SHA to running image.
- Technical gates fail closed with deterministic evidence.
- Backups and compatibility checks make rollback claims testable.
- Mutable registry state and dirty worktrees cannot silently alter a release.

**Negative**

- Releases require more artifacts, validation, storage, and operator time.
- A harmless-looking repository, image, or live-state mismatch blocks release.
- Schema-changing releases need separate migration and rollback reasoning.
- Emergency work still cannot bypass technical evidence without accepting an
  explicitly different and reviewed contract.
