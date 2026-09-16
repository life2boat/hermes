# Deployment provenance

Operate only from the canonical repository and trusted remote. Require clean
`HEAD` to equal the exact 40-character resolved `main` SHA; branch names and
worktree contents are mutable. Record the resolved `main` SHA and a passing
`check-repository` result.

Build from a verified export of the exact Git tree, never a raw worktree:
ignored or untracked files can contaminate a raw build context. Deploy only an
immutable image ID or registry digest. Mutable tags can move after review; the
single OCI revision equals the source SHA and tag text never proves provenance.

Use `scripts/hermes_production_deploy.sh check-repository` and the canonical
deployment runbook. A clean local checkout, CI result, or image pull does not
grant production authority.
