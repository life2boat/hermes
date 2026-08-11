# Agent Release Gates

Status: normative merge and production-release semantics
Scope: AI-assisted Hermes repository and release decisions

## Core invariant

```text
CODE PASS != PRODUCTION RELEASE PASS
```

Green code CI proves only the tested source contract. It does not prove that an
agent respected authority, that security and cost evidence exist, that the
current production baseline is healthy, or that production mutation is
authorized.

## Gate types

| Gate | Question | Typical evidence |
| --- | --- | --- |
| CODE_GATE | Does the changed implementation/document contract pass required code and repository checks? | Focused tests, regressions, lint/type checks, exact-head CI. |
| BEHAVIOUR_GATE | Did the agent follow required authority, scope, status, and stop-boundary behaviour? | Required deterministic evals and, only when classified, live evals. |
| SECURITY_GATE | Are required identity, secret, tool, data, and release invariants proven? | Security/adversarial cases, scans, fixed-schema evidence. |
| COST_GATE | Is required call/token/output/estimated-cost evidence within the task budget? | Versioned budget receipt. |
| PRODUCTION_READINESS_GATE | Is the exact candidate safe and authorized for the current production state? | Production readiness checklist and applicable procedural skill evidence. |

## Merge versus production release

The default contract is:

```text
MERGE_ELIGIBLE =
    CODE_PASS
    AND REQUIRED_OFFLINE_BEHAVIOUR_PASS
    AND REQUIRED_SECURITY_PASS
```

```text
PRODUCTION_RELEASE_ELIGIBLE =
    MERGE_ELIGIBLE
    AND REQUIRED_LIVE_BEHAVIOUR_PASS
    AND REQUIRED_COST_PASS
    AND PRODUCTION_READINESS_PASS
```

Task classification determines which gates are required. A trivial docs PR
does not automatically require live behaviour, production readiness, or a cost
benchmark. A security-sensitive agent change can require offline behaviour and
security gates even when no production release is requested.

`MERGE_ELIGIBLE` and `PRODUCTION_RELEASE_ELIGIBLE` are distinct decisions. A
merged change can remain ineligible for build, deploy, feature activation, or
live smoke.

## Status semantics

Every required gate uses the repository taxonomy:

```text
PASS | FAIL | BLOCKED | NOT_RUN | NOT_PERFORMED | UNKNOWN | INCONCLUSIVE
```

Only explicit PASS satisfies a required gate. `UNKNOWN`, `NOT_RUN`,
`NOT_PERFORMED`, and `INCONCLUSIVE` remain visible and cannot be aggregated into
PASS. Missing required evidence closes the decision.
The explicit identities `UNKNOWN != PASS`, `NOT_RUN != PASS`, and
`INCONCLUSIVE != PASS` prevent incomplete evidence from being normalized into
a release success.

## Technical blockers and governance observations

Technical blockers include canonical provenance or exact-SHA mismatch,
relevant test/CI failure, secret exposure, authority violation, unsafe data
path, integrity failure, security invariant failure, required behaviour/cost
gate failure, and unproven rollback when rollback is required.

The following are governance observations by themselves, not technical
blockers: branch protection disabled, an optional independent review missing,
nonessential PR metadata, or an optional documentation field. Governance
classification never waives a failed or unknown technical gate.

## Evidence aggregation

A release report must name task classification, required gates, exact source
and candidate identity, each gate status, evidence references, governance
observations, technical blockers, and the final decision. It must not infer a
gate from another gate: code PASS is not behaviour PASS, and merge eligibility
is not production authority.

## Implementation state

These semantics are authoritative documentation. The executable release-gate
aggregator and CI behaviour gate are `PLANNED`; production remains governed by
the current task, versioned deployment policy, procedural skills, and existing
technical gates until separate implementation PRs land.
