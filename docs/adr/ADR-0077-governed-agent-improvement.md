# ADR-0077: Governed Agent Improvement through Repository Candidates

Status: Accepted

Date: 2026-08-11

## Problem

Agent failures can reveal valuable improvements, but direct self-modification of
production policy or workflows would bypass review, tests, authority, and
rollback controls.

## Context

Hermes already captures sanitized failures and uses skills, ADRs, tests, and
Git review as durable decision memory. Repeated procedures may eventually be
encoded as deterministic graphs, but only after their intent, sequence, failure
modes, side effects, and authority boundaries stabilize.

## Decision

Agent self-improvement may create repository candidates; it may not directly
mutate or activate production policy.

The authoritative lifecycle is defined in
[`docs/SKILL_LOOP_GRAPH_LIFECYCLE.md`](../SKILL_LOOP_GRAPH_LIFECYCLE.md):
failure -> sanitized trace -> candidate improvement -> eval -> PR -> CI/review
-> merge. Graph promotion additionally requires evidence of repeatable stable
behaviour and an adequate regression corpus.

## Alternatives Considered

- **Allow automatic production-policy rewrites after failures.** Rejected
  because a single noisy outcome could expand authority or remove a safety gate.
- **Disallow agent-generated improvements entirely.** Rejected because agents
  can efficiently propose useful tests, skills, and workflow candidates under
  normal review.
- **Promote every written procedure directly into a graph.** Rejected because
  existence does not prove maturity or deterministic semantics.

## Consequences (+ and -)

Positive:

- failure learning remains fast while preserving repository governance;
- improvements gain reviewable provenance and deterministic regression evidence;
- production policy cannot drift through unreviewed self-modification.

Negative:

- improvements take longer to activate;
- traces and eval datasets require sanitization and maintenance;
- some procedures remain agent loops until sufficient maturity evidence exists.

## Testing Implications

Later tooling must prove candidate-only status before merge, deny direct
activation, bind promotion to reviewed evidence, and preserve the original
authority and side-effect boundaries after graph hardening.
