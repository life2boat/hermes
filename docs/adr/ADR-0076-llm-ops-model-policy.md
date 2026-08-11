# ADR-0076: Versioned LLM Ops Model Policy

Status: Accepted

Date: 2026-08-11

## Problem

Ad hoc model selection makes engineering quality, cost, and risk difficult to
reproduce. It can also create the false impression that a stronger model has
more authority than the task grants.

## Context

Hermes tasks range from bounded repository discovery to high-risk production
migration. They need different reasoning capability and budgets, while model
availability and provider pricing can change independently of repository
policy.

## Decision

Model selection is governed by the versioned task policy in
[`docs/LLM_OPS_POLICY.md`](../LLM_OPS_POLICY.md).

Task classification recommends a model tier and reasoning level. The selection
must preserve the task's scope, authority, privacy, and stop boundary. Tasks may
bind maximum model calls, input tokens, output tokens, and estimated cost.
Provider pricing is an external versioned input, not a hardcoded normative
constant.

## Alternatives Considered

- **Let each agent choose by preference.** Rejected because results and cost are
  not reproducible or reviewable.
- **Always use the strongest model.** Rejected because it wastes budget and
  still does not supply missing authority or evidence.
- **Hardcode provider prices in the policy.** Rejected because prices change
  independently and would make the contract stale.

## Consequences (+ and -)

Positive:

- model choice and substitution become explicit evidence;
- cost budgets can be evaluated without weakening safety;
- higher capability cannot be misread as broader permission.

Negative:

- the matrix needs versioned maintenance as models evolve;
- unavailable recommended tiers require explicit substitution handling;
- accurate estimated-cost gates need an external pricing-source contract.

## Testing Implications

Future policy tests must cover each task class, unavailable recommendations,
substitution, budget overrun, hidden retries/fallbacks, unknown pricing, and the
invariant that model tier never changes allowed effect classes.
