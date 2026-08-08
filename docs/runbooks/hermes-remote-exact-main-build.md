# Hermes remote exact-main image build

This runbook defines the only approved off-host build path for the Healbite
production candidate. It publishes an immutable SHA tag and reports a registry
digest. It never pulls an image to the production VPS and never deploys.

## Authority and trigger

The workflow is `.github/workflows/healbite-exact-main-ghcr.yml`. It is manual
only and can publish only when GitHub reports both the repository
`life2boat/hermes` and `refs/heads/main`. The workflow has `contents: read` and
`packages: write`; it has no pull-request trigger or user-controlled source-ref
input.

Dispatch from the canonical repository with the default branch explicitly:

```bash
gh workflow run healbite-exact-main-ghcr.yml \
  --repo life2boat/hermes \
  --ref main
```

The job checks out the event SHA with full history, fetches only canonical
`origin/main`, and fails unless checkout HEAD, `GITHUB_SHA`, and fetched main
are identical. If main moves between dispatch and the gate, the build is
denied.

## Reviewed artifact acquisition

`deploy/playwright-remote-build-artifacts.json` is the versioned approval
policy. It binds the exact lock-selected Playwright wheel, the reviewed
Chromium and FFmpeg archive sizes and SHA-256 identities, opaque approval
references, the expected aggregate closure digest, platform, and approved base
commit. It contains no credential.

`scripts/prepare_remote_playwright_artifacts.py` materializes those public
artifacts only in the hosted runner's private temporary directory. Downloads
are HTTPS-only, host-restricted, size-bounded, and digest-verified. The script
derives package and artifact metadata from the verified wheel, reconstructs
canonical `closure.json`, and requires its byte digest to equal the reviewed
aggregate digest. Acquisition and build remain separate workflow steps.

## One exact-tree build

The workflow invokes the canonical helper once in `build-push` mode. The helper:

1. verifies the exact commit and approved-base ancestry;
2. scans candidate Git objects for secrets;
3. exports and re-reads the exact Git tree;
4. verifies the complete Playwright named context;
5. invokes one `docker buildx build --push`;
6. binds the full source SHA to the embedded build marker and OCI revision
   label;
7. binds the canonical repository to the OCI source label;
8. writes a safe build receipt and registry digest.

No production secret or production dotenv path is an input. `latest` is never
created or treated as release authority.

## Registry and full-image attestation

After the push, the hosted runner resolves the digest with Buildx registry
inspection. `scripts/attest_remote_registry_image.py` verifies:

- digest identity of the raw manifest;
- single `linux/amd64` image configuration;
- OCI revision equals the exact main SHA;
- OCI source equals the canonical repository;
- build receipt source/context identities;
- compressed layer total and config digest;
- zero secret findings in image configuration and history.

The hosted runner then pulls that same immutable digest and runs
`scripts/hermes_image_secret_scan.py`. Source scanning alone cannot prove the
built artifact contains no recoverable credential, and a final-filesystem-only
scan misses bytes deleted by later whiteouts. The canonical scanner therefore
binds the local image ID and exact OCI revision, verifies Docker/OCI config and
layer identities, scans configuration, environment, labels, history, every
stored layer, and the resulting filesystem, and fails closed on malformed,
missing, ambiguous, unsafe, or unsupported content.

The workflow publishes `IMAGE_SECRET_FINDINGS=0` only after all narrower
metadata, layer, and final-filesystem counts are zero. It uploads only safe
receipts with counts, identities, policy hash, and hashed paths; authentication
tokens, matching bytes, raw configuration, and image contents are not uploaded.

## Production-host stop boundary

After the workflow succeeds, inspect the digest and compressed layer metadata
without pulling it to the production VPS. Apply the manifest's deploy-capacity
policy, protect the running and proven rollback images, and stop after
classifying the deploy capacity gate. Pull and deployment require a separate
authorized task.
