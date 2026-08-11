# ADR-0074: Agent Behaviour as a First-Class Engineering Contract

Status: Accepted

Date: 2026-08-11

## Problem

Code tests can prove that a function or command worked while missing whether an
agent was authorized to perform the action, stayed in scope, handled unknowns
truthfully, or stopped at the required boundary. That gap allows technically
successful but unsafe AI-assisted changes.

## Context

Hermes v1 already defines task preparation, provenance, invariants, AI review,
production readiness, and an exact stop boundary. Those controls need one
authoritative behaviour definition so future eval and release tooling does not
invent incompatible meanings of correct agent conduct.

For example, a successful refund API response does not prove refund authority;
similarly, a successful Git merge does not prove that a Draft-PR task authorized
merge.

## Decision

Agent behaviour is a first-class engineering contract owned by
[`docs/AGENT_BEHAVIOUR_CONTRACT.md`](../AGENT_BEHAVIOUR_CONTRACT.md).

The contract covers provenance, authority, scope, stop boundary, truthfulness,
unknown handling, tool selection and safety, secret handling, failure handling,
model selection, and cost discipline. Required behaviour evidence is evaluated
separately from code correctness. A required unknown, not-run, or inconclusive
result never becomes PASS.

## Alternatives Considered

- **Treat green code CI as sufficient agent evidence.** Rejected because CI
  does not prove authority, scope, or stop-boundary compliance.
- **Keep behaviour guidance only in prompts or chat.** Rejected because those
  sources are not stable, reviewable repository contracts.
- **Encode behaviour directly in a runner before defining semantics.** Rejected
  because implementation would harden ambiguous or conflicting policy.

## Consequences (+ and -)

Positive:

- future evals and release gates share one behaviour vocabulary;
- technically successful unauthorized actions can be classified correctly;
- evidence and unknown-handling requirements become reviewable.

Negative:

- changes may require behaviour evidence in addition to code tests;
- the contract and eval corpus need maintenance as task classes evolve;
- documentation alone does not enforce behaviour until later tooling lands.

## Testing Implications

Later PRs must add deterministic component, scenario, and adversarial cases for
every required dimension. Tests must verify protected effects remain zero on
authority, scope, and stop-boundary failures. LLM-as-judge cannot be the sole
judge for critical safety outcomes.
