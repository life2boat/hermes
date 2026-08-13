# Food-Vision receipt observability gap

## Proven facts

- The evidence is bound to source SHA `fd41a0d60f2b68537f331de385455618e98ea3f5`.
- The bounded benchmark used provider `alibaba`, model `qwen3.6-flash`, fixture set `food_vision_quality_v1`, and manifest SHA `7d946a450e84471345114ff1c31dc058289de6e8a87128353bee96a9c9a57505`.
- Exactly three requests were used with zero retries and zero cross-provider fallbacks.
- Model access, vision capability and Hermes schema compatibility passed; precision, recall and sauce recall were all `0.0`.
- The original receipt retained counts but not prediction identities, true-positive counts or unmatched identities.

## Root-cause status

The quality failure remains **INCONCLUSIVE**. Receipt v2 now preserves
sanitized match/miss/unexpected classifications, but the same immutable v1 bytes
can still produce materially different semantic predictions across executions.
Neither model capability failure, scorer failure, nor fixture invalidity is
therefore established. The historical v1 set was never proven representative of
real food photography.

## Resolution and lesson

Add bounded diagnostics derived only from schema-validated inventory labels.
Never persist raw provider responses, image data, credentials, request
identifiers, or user identifiers. Counts-only quality receipts cannot explain a
zero-score result; evidence must preserve safe match/miss/unexpected
classifications. The follow-up `food_vision_quality_v2` fixture set adds
purpose-created photorealistic scenes while retaining exact hashes, the same
scoring thresholds, and the strict no-request dry-run contract. It is a more
representative candidate benchmark, not proof of production performance.

A second bounded gap was proven by the immutable version-2 replacement receipt:
schema-invalid fixtures retained no validator reason, so malformed JSON, field
shape errors, and local invariant rejections could not be distinguished after
the provider response was discarded. Receipt version 3 closes that gap using
only the local validator's closed reason code and a coarse static trigger class.
It still never persists provider output or any request payload. Historical
version-2 evidence remains immutable; its missing reasons cannot be reconstructed.
