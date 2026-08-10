# Canonical Qwen Vision quality harness

`scripts/run_food_vision_quality.py` is the non-production eligibility gate
for an explicit DashScope Vision model. It is a provider-executing contract,
not a runtime activation mechanism: it cannot select a default model, alter a
feature flag, or authorize deployment.

## Fixture and request contract

The versioned fixture set is `food_vision_quality_v1` at
`tests/fixtures/food_vision_quality/v1/manifest.json`. It contains exactly
three deterministic, purpose-created synthetic PNG fixtures. The manifest binds
each fixture ID to a SHA-256 and expected components; a mismatch fails before
any provider request.

The request budget is exactly the number of enabled manifest fixtures: v1 is
three fixtures and therefore at most three external requests. Each fixture gets
one `VISION_SINGLE_REQUEST_LLM_CALL_POLICY` call. Retries, credential recovery
retries, model refreshes, and cross-provider fallbacks are all disabled.

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

The receipt contains only versioned fixture IDs and hashes, static request/error
classes, counts, aggregate metrics, and timestamps. It never includes image
bytes, data URLs, absolute paths, prompt text, raw provider responses, headers,
or credentials.

Eligibility passes only when all hashes pass, all three requests are used once,
the provider response is application-schema valid for every fixture, and:

- precision is at least 0.90;
- recall is at least 0.90;
- sauce recall is at least 0.90;
- unsafe aggregate-nutrition outputs equal zero; and
- invalid outputs equal zero.

The harness reports evidence only. A successful candidate run requires a
separate, minimal decision change before any model can become rollout eligible.
