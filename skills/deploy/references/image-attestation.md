# Image attestation and secret coverage

An image is release-eligible only when its immutable digest/ID has the exact
`org.opencontainers.image.revision` label matching the selected source SHA.
Require the canonical full-image `scripts/hermes_image_secret_scan.py` receipt:
it covers configuration, metadata, labels, history, every recoverable layer,
and the final filesystem.

Source scanning cannot prove artifact cleanliness. A missing, malformed,
ambiguous, unsafe, or uninspectable archive member is a technical failure, not
zero findings. Bind the receipt to the immutable image ID, exact OCI revision,
ordered layer identities, and scan-policy hash. Use
`scripts/attest_remote_registry_image.py` for registry identity evidence.
