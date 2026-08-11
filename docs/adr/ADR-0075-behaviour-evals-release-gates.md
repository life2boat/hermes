# ADR-0075: Separate Behaviour Evals from Production Release Gates

Status: Accepted

Date: 2026-08-11

## Problem

Green code CI is insufficient evidence for an AI-agent production release. It
does not prove correct authority handling, required live behaviour, security,
cost limits, current production readiness, or rollback safety.

## Context

Hermes already separates exact-head CI from production authority and uses
fail-closed deployment gates. Agent behaviour adds another evidence class that
must be explicit without forcing every trivial repository change through live
provider or production tests.

## Decision

Adopt separate gate semantics owned by
[`docs/AGENT_RELEASE_GATES.md`](../AGENT_RELEASE_GATES.md) and evaluation
methodology owned by [`docs/BEHAVIOUR_EVALS.md`](../BEHAVIOUR_EVALS.md).

Merge eligibility requires code PASS plus all task-required offline behaviour
and security evidence. Production release eligibility additionally requires all
task-required live behaviour, cost, and production-readiness evidence.

Task classification determines which gates apply. Missing required evidence
closes its gate. Merge eligibility never implies production release authority.

## Alternatives Considered

- **Use one green/red release flag.** Rejected because it hides which evidence
  is missing and conflates merge with production authorization.
- **Require every gate for every PR.** Rejected because docs-only and trivial
  changes do not need live behaviour or production readiness.
- **Let a live LLM judge all behaviour.** Rejected because critical safety gates
  must remain deterministic and available offline.

## Consequences (+ and -)

Positive:

- merge and production decisions remain explicit and independently reviewable;
- safety evidence cannot be inferred from unrelated green checks;
- low-risk changes can use proportionate gate profiles.

Negative:

- task classification becomes a maintained policy surface;
- release reports contain more statuses and evidence bindings;
- executable aggregation and CI integration require later PRs.

## Testing Implications

Gate tests must cover required, optional, missing, failed, blocked, and
inconclusive evidence. They must prove that `UNKNOWN`, `NOT_RUN`, and
`INCONCLUSIVE` cannot satisfy a required gate and that governance observations
cannot waive technical failures.
