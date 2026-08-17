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
| BEHAVIOUR_GATE | Did the agent follow required authority, scope, status, and stop-boundary behaviour offline? | Required deterministic GOLDEN eval evidence. |
| SECURITY_GATE | Are required identity, secret, tool, data, and release invariants proven? | Security/adversarial cases, scans, fixed-schema evidence. |
| LIVE_BEHAVIOUR_GATE | Did the exact release candidate pass separately authorized live behaviour checks? | Sanitized fixed-schema live evidence; never inferred from offline evals. |
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

## Executable decision layer

The offline contract has explicit identities:

```text
RELEASE_GATE_SCHEMA_VERSION=1
RELEASE_GATE_POLICY_VERSION=1
```

`ai_engineering/release_gate.py` implements the closed target and gate
taxonomies, deterministic requirement derivation, typed source/evidence,
technical blocker and governance observation records, canonical JSON, and
receipt SHA-256 identity. `scripts/check_agent_release_gate.py` exposes
`evaluate` and conservative `ci-merge` modes.

The target plus sensitivity classification derives required gates. A caller
cannot mark a derived required gate optional. Every required gate needs its own
explicit PASS; FAIL produces FAIL, while BLOCKED, UNKNOWN, NOT_RUN,
NOT_PERFORMED, or INCONCLUSIVE produces BLOCKED. Governance observations stay
visible without changing either eligibility decision.

For `target=MERGE`, the production decision is always reported as
`NOT_PERFORMED`. For `target=PRODUCTION_RELEASE`, merge eligibility plus every
derived live, cost, and production-readiness gate is evaluated. This aggregation
does not grant production authority or perform a production operation.

The `Agent Release Gate` pull-request workflow checks out
`github.event.pull_request.head.sha` directly with read-only permissions and
full history. Its conservative merge profile runs `agent_check.sh`, the full
GOLDEN behaviour corpus, an independent exact base-to-candidate Git-tree
secret scan, and the adversarial
behaviour category. Cost, live behaviour, and production readiness remain
visible as optional `NOT_PERFORMED` evidence for this merge-only decision.

## Implementation state

The executable release aggregator, merge/production decision receipts, CLI,
and exact-head merge CI are `IMPLEMENTED`. The CI workflow has no provider or
production credentials and performs no live smoke, deploy, runtime, database,
Qdrant, or secret mutation. Production release remains governed by the current
task, explicit authority, versioned deployment policy, procedural skills, and
separately supplied live/cost/readiness evidence. Failure-to-Eval candidates
and procedure-maturity receipts are offline repository evidence; neither can
grant merge, production-release, deployment, runtime, data, vector, or secret
mutation authority.

## Production Readiness Evidence Bridge (C1)

Sprint C1 implements a deterministic, offline evidence bridge that binds a
verified `ProductionRuntimeAttestation` + `ProductionRuntimeComparison` pair
to the `PRODUCTION_READINESS_GATE`.

### Evidence pipeline

```text
ProductionRuntimeAttestation
        ↓ (canonical validator)
ProductionRuntimeComparison
        ↓ (binding + freshness + post-health checks)
ProductionReadinessEvidenceReceipt
        ↓ (deterministic adapter)
GateEvidence(PRODUCTION_READINESS_GATE)
        ↓
ReleaseGateReceipt
```

### MATCH alone is not enough

`comparison=MATCH` alone does NOT produce `PRODUCTION_READINESS_PASS`.

A production readiness PASS additionally requires all of:

- valid canonical attestation (from `validate_attestation`);
- valid canonical comparison (from `validate_comparison`);
- attestation/comparison ID binding;
- expected target binding;
- `candidate_sha == observed_head_sha` (exact-SHA semantics);
- valid `runtime_evidence_source_sha` (not necessarily == production_sha);
- evidence freshness (`age <= max_age_seconds`; no `datetime.now()` in core);
- `post_collection_health_status == PASS`.

### Fail-closed semantics

| Condition | Status |
|---|---|
| `comparison=MATCH` + `post_health=PASS` + all identity checks valid + fresh | `PASS` |
| `comparison=DRIFT` | `FAIL` (reason: `PRODUCTION_RUNTIME_DRIFT`) |
| `comparison=INSUFFICIENT_EVIDENCE` | `BLOCKED` (reason: `PRODUCTION_RUNTIME_EVIDENCE_INSUFFICIENT`) |
| `post_health=FAIL` | `FAIL` (reason: `POST_COLLECTION_HEALTH_FAIL`) |
| `post_health=INSUFFICIENT_EVIDENCE` | `BLOCKED` (reason: `POST_COLLECTION_HEALTH_INSUFFICIENT_EVIDENCE`) |
| stale evidence | `BLOCKED` (reason: `PRODUCTION_RUNTIME_EVIDENCE_STALE`) |
| candidate/head mismatch | `FAIL` (reason: `EXACT_SHA_MISMATCH`) |

Historical B2 regression: `MATCH + INSUFFICIENT_EVIDENCE → BLOCKED`. This
cannot be retroactively upgraded.

### Authority boundary

```text
PRODUCTION_READINESS_PASS != PRODUCTION_EXECUTION_AUTHORIZED
EVIDENCE_EXPANDS_AUTHORITY = false
```

The bridge proves evidence quality and readiness only. It does not deploy,
restart containers, access production, read secrets, or grant execution authority.

### Implementation

- `ai_engineering/production_readiness_evidence.py` — offline verifier, receipt
  contract, and `GateEvidence` adapter.
- `schemas/production-readiness-evidence-v1.schema.json` — structural JSON Schema.
- `scripts/check_production_readiness_evidence.py` — offline CLI; file-in/file-out only.
- `tests/test_production_readiness_evidence.py` — 27-case mandatory test suite.
