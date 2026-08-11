# Food-Vision receipt observability gap

## Proven facts

- The evidence is bound to source SHA `fd41a0d60f2b68537f331de385455618e98ea3f5`.
- The bounded benchmark used provider `alibaba`, model `qwen3.6-flash`, fixture set `food_vision_quality_v1`, and manifest SHA `7d946a450e84471345114ff1c31dc058289de6e8a87128353bee96a9c9a57505`.
- Exactly three requests were used with zero retries and zero cross-provider fallbacks.
- Model access, vision capability and Hermes schema compatibility passed; precision, recall and sauce recall were all `0.0`.
- The original receipt retained counts but not prediction identities, true-positive counts or unmatched identities.

## Root-cause status

The quality failure remains **INCONCLUSIVE**. The proven defect was insufficient sanitized receipt observability; neither model capability failure, scorer failure nor fixture validity failure is established. The fixtures' real-photo representativeness remains an open question.

## Resolution and lesson

Add bounded diagnostics derived only from schema-validated inventory labels. Never persist raw provider responses, image data, credentials, request identifiers or user identifiers. Counts-only quality receipts cannot explain a zero-score result; future evidence must preserve safe match/miss/unexpected classifications.
