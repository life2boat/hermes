# Failure Knowledge

This directory stores sanitized engineering failure records. Each record should
capture the technical cause, the verified resolution, and the reusable lesson
without exposing secrets, production data, raw logs, personal identifiers, or
unverified incident claims.

Records are evidence, not operational authority. Link remediation rules to the
relevant tests, policies, skills, or runbooks, and state when a conclusion is an
inference rather than a proven fact.

Use [`docs/FAILURE_CAPTURE_LOOP.md`](../../docs/FAILURE_CAPTURE_LOOP.md) after
every serious incident. It defines the required evidence, decision-memory and
hardening sequence; this directory holds its sanitized, reviewable records.

Initial record:

- [Image scanner rejected an absolute rootfs symlink](failure_image_scanner_absolute_symlink.md)

- [Food-Vision receipt observability gap](failure_food_vision_receipt_observability_gap.md)
