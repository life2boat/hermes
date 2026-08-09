# Image Scanner Rejected an Absolute Rootfs Symlink

## Cause

The image secret scanner originally applied archive-member path rules to both
member names and symbolic-link targets. A legitimate absolute target inside the
container root filesystem, such as `/usr/bin/tini -> /init`, was therefore
classified as `IMAGE_ARCHIVE_PATH_UNSAFE`. The scanner failed closed, but the
structural rejection prevented the exact-main image scan from reaching a secret
findings verdict.

## Resolution

The scanner introduced a dedicated symlink-target validator. Absolute symlink
targets are resolved within the image root, relative targets are resolved from
the symlink parent, and attempts to escape above the image root remain denied.
Hardlink targets continue to use strict archive-root validation.

Regression tests cover legitimate in-root symlinks, above-root traversal, and
absolute hardlink rejection. The workflow also records a fixed-schema,
sanitized failure receipt so a structural failure can be diagnosed without raw
paths, targets, matched values, or exception text.

## Lesson Learned

Archive member names, symbolic-link targets, and hardlink targets have distinct
filesystem semantics and require separate trust rules. Supply-chain scanners
should remain fail-closed while preserving sanitized failure evidence, and
their fixtures must include normal container rootfs structures as well as
malicious traversal cases.
