# Prompt Evaluation Contract

Status: normative evaluation contract

## Deterministic-first evaluation

Prompt quality is evaluated offline before provider or production use whenever
the contract can be checked deterministically. Critical trust, output, failure,
and capability outcomes must not depend solely on an LLM reviewer.

The provider-free runner at `scripts/run_prompt_quality_evals.py` loads the
closed corpus under `evals/prompt_quality`, validates exact fixture schemas,
compiles valid specs, compares exact diagnostics and section oracles, and emits
a deterministic sanitized receipt. Exit codes are `0=PASS`, `1=FAIL`, and
`2=BLOCKED/configuration error`.

## Minimum corpus

The candidate corpus includes exactly reviewed synthetic cases for:

- repository repair;
- ambiguous user request;
- missing required evidence;
- structured extraction;
- untrusted prompt injection;
- multi-step execution order;
- missing failure behaviour;
- a historical contradictory-scope regression.

Fixtures contain no secrets, private identifiers, raw production evidence,
provider responses, or real user messages. Corpus content is identified by a
canonical SHA-256 digest over the immutable manifest projection and JSONL
cases. `CANDIDATE` does not mean `GOLDEN`; human review and a digest-bound
promotion remain separate from technical CI.

## Quality gates

A case passes only when validity, ordered error codes, ordered warning codes,
required compiled sections, and trust-boundary checks match its oracle. A
required mismatch is `FAIL`. Malformed, unsafe, missing, duplicate-key, or
unsupported corpus data is `BLOCKED`. Warnings do not block unless they denote
a safety or integrity requirement promoted to an error by the contract.

Prompt changes must run focused compiler, validator, linter, injection,
output, failure, trace, and corpus tests. A reproduced prompt failure should
be classified, corrected in the smallest relevant `PromptSpec` component, and
retained as a deterministic regression case.

## Behaviour-eval relationship

Prompt eval proves construction and quality-policy conformance. Behaviour eval
proves whether an agent acted correctly in context. Neither result implies the
other, and neither grants deployment authority. Prompt provenance may be
attached to sanitized Behaviour Trace schema v2 so a behavioural result can be
bound to the exact prompt artifact without storing prompt text.
