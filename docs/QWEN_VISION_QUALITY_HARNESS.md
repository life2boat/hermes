# Canonical Qwen Vision quality harness

`scripts/run_food_vision_quality.py` is the non-production eligibility gate
for an explicit DashScope Vision model. It is a provider-executing contract,
not a runtime activation mechanism: it cannot select a default model, alter a
feature flag, or authorize deployment.

## Fixture and request contract

The harness supports only the explicit fixture-set allowlist
`food_vision_quality_v1`, `food_vision_quality_v2`, and
`food_vision_quality_v3`. Each manifest binds exactly three fixture IDs to
SHA-256 hashes and expected components; a manifest or image mismatch fails
before any provider request.

`v1` is immutable historical geometric-synthetic evidence, retained despite its replay-instability and representativeness concern. `v2` at
`tests/fixtures/food_vision_quality/v2/manifest.json` contains purpose-created,
photorealistic synthetic scenes with an ordinary-food scene, a non-food
distractor scene, and a condiment scene. It improves candidate visual
representativeness, but does not prove real-world accuracy or rollout
eligibility. The v2 provenance record is kept beside its manifest.

V3 is a `CANDIDATE` successor. It reuses the exact v2 image hashes without
copying or mutating the PNGs. A and B retain their recognition/distractor roles.
C accepts the runtime-canonical generic `sauce` class for the visually ambiguous
white condiment, requires clarification, and rejects plausible narrower labels
as unsupported specificity. Exact ketchup and generic yellow-sauce recognition
remain required. No fourth fixture is needed because A/B already exercise exact
recognition and C includes both resolvable and ambiguous condiment evidence.
The v3 manifest and review package require a separate digest-bound human visual
review before provider execution may treat the candidate truth as reviewed.

The request budget is exactly the number of enabled manifest fixtures: all
supported versions contain three fixtures and therefore allow at most three
external requests. Each fixture gets one `VISION_SINGLE_REQUEST_LLM_CALL_POLICY`
call. Retries, credential recovery retries, model refreshes, and cross-provider
fallbacks are all disabled.

## Provider and credential boundary

The harness accepts only `--provider alibaba` and requires a non-empty explicit
`--model`. It reads only `DASHSCOPE_API_KEY` from the invoking process when
`--execute-provider` is present. It has no CLI key parameter and never reads
`QWEN_API_KEY`; the credential is not copied, hashed, logged, or written to a
receipt.

Without `--execute-provider`, the command is a dry run. It validates the
manifest and hashes, writes a `DRY_RUN` receipt, and performs zero network
requests. Normal CI must use this default behavior.

## Safe invocation

```text
python scripts/run_food_vision_quality.py \
  --provider alibaba \
  --model qwen3.6-flash \
  --fixture-manifest tests/fixtures/food_vision_quality/v1/manifest.json \
  --receipt-out /private/evidence/qwen-quality-v1.json \
  --execute-provider
```

The receipt path must be new. Store execution evidence outside a Git worktree.

## Decision rule

The version-3 receipt contains versioned fixture IDs and hashes, static
request/error classes, bounded schema-validated prediction diagnostics, closed
validator reason codes and coarse trigger summaries, counts, aggregate metrics,
and timestamps. Diagnostics expose only sanitized labels, canonical
match/miss/unexpected sets, and values from the local validator's closed reason
registry; they never include image bytes, data URLs, absolute paths, prompt text,
raw provider responses, headers, request IDs, or credentials. V3 product
outcomes use the existing per-fixture `request_status_class`: recognition
correct, safely clarified ambiguity, unsupported specificity, recognition miss,
or schema-invalid/unsafe output. This additive routing does not change receipt
schema version 3. Historical version-1 and version-2 receipts remain valid
evidence, but a version-2 SCHEMA_INVALID result does not identify its exact
validator trigger.

Eligibility passes only when all hashes pass, all three requests are used once,
the provider response is application-schema valid for every fixture, and:

- precision is at least 0.90;
- recall is at least 0.90;
- sauce recall is at least 0.90;
- unsafe aggregate-nutrition outputs equal zero; and
- invalid outputs equal zero; and
- every v3 fixture has product outcome `RECOGNITION_CORRECT` or
  `AMBIGUOUS_BUT_SAFELY_CLARIFIED`.

The harness reports evidence only. A successful candidate run requires a
separate, minimal decision change before any model can become rollout eligible.
