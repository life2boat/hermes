# ADR-0078: Prompt Specifications as Versioned Engineering Artifacts

Status: Accepted

Date: 2026-08-12

## Problem

Large prompts assembled as ad hoc text hide missing context, conflicting
constraints, trust-boundary mistakes, unsupported output assumptions, and
unreviewed behavioural changes. Code review cannot reliably reason about a
prompt whose structure and provenance are implicit.

## Context

Hermes v2 already has deterministic behaviour traces, replay, evals, model and
cost policy, release gates, and a governed failure-to-regression lifecycle.
Prompt construction must integrate with these controls without creating a new
runtime framework or persisting raw prompts and private reasoning as evidence.

## Decision

Complex prompts are typed `PromptSpec` artifacts in the existing stdlib-only
`ai_engineering` package. A provider-free compiler emits ordered tagged or
Markdown sections, excludes empty content, selects canonical context, escapes
untrusted dynamic input, and records versioned provenance. A deterministic
validator and linter fail closed for missing complex-task contracts, trust
boundary violations, raw chain-of-thought requests, contradictions, and
unsupported structured-output assumptions.

Prompt quality uses a separate sanitized candidate corpus and offline runner.
Behaviour Trace schema v2 may record prompt identity/digest/eval metadata but
never prompt text, hidden reasoning, secrets, or raw user/provider payloads.
Trace schema v1 remains readable and byte-compatible.

## Alternatives Considered

- **Continue free-form authoring.** Rejected because quality depends on the
  individual author and regressions are difficult to identify.
- **Use an LLM prompt reviewer as the gate.** Rejected for critical outcomes;
  deterministic contracts and oracles are reproducible and provider-free.
- **Create a separate prompt service/framework.** Rejected because the existing
  engineering-control layer already owns contracts, traces, evals, and gates.
- **Persist complete prompts in traces.** Rejected because prompts may contain
  untrusted, private, or secret material and raw prompt text is unnecessary for
  provenance identity.

## Consequences (+ and -)

Positive:

- prompt intent, ordering, constraints, failure, and output become reviewable;
- injection boundaries and capability assumptions fail closed;
- prompt changes have deterministic digests and regression evidence;
- trace evidence can identify an artifact without retaining raw content.

Negative:

- complex prompt authors must maintain typed fields and eval fixtures;
- provider capability metadata must remain current;
- the initial corpus is candidate-only until independently reviewed for GOLDEN
  promotion.

## Testing Implications

Tests must cover deterministic compilation, input isolation, missing required
sections/evidence, contradictions, raw-CoT denial, provider capability limits,
output schemas, trace v1/v2 compatibility, corpus identity, safe receipt
creation, and the failure-to-regression taxonomy.
