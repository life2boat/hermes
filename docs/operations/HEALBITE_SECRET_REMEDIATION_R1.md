# HealBite Secret Remediation R1

This repository-only clean-forward closure hardens remediation tooling without executing it against production.

## Provenance boundary

- `PR179_MERGE_SHA=e85ca7dbee2025320c5daf61181a6c1142f18a9b`
- `FORENSIC_REFERENCE_SHA=f438671ee445ae5a73a2aad235298fe5f1439536`
- `PRODUCTION_FORENSIC_RUNTIME_STATE=PASS`
- `HISTORICAL_EXECUTION_PROVENANCE=UNPROVEN`
- `PRODUCTION_REPAIR_REQUIRED=false`
- `FOLLOW_UP_PURPOSE=canonical source closure only`

The forensic reference proves an observed runtime state, not the exact historical source that executed. This closure independently implements justified behavior from canonical main; it does not cherry-pick F438 or claim it as production provenance.

## Change classification

| Class | Closure |
| --- | --- |
| PORT | Bounded poller convergence, container-scoped gateway health, exact child Compose bindings. |
| REDESIGN | Verified-process protected-name capture before mutation and duplicate-rejecting JSON byte-span removal. |
| REJECT | Whole-document JSON reserialization, compose-label image authority, ambient protected interpolation, unbounded polling, raw subprocess diagnostics, inferred historical provenance. |

## Critical contracts

- The Compose child environment scrubs every protected name, binds `HERMES_IMAGE` to the approved immutable legacy reference, and binds `HERMES_GIT_SHA` to the exact worktree commit without mutating the parent environment.
- Source validation runs `docker compose config --no-interpolate --format json` over the exact eight-file stack.
- Pre-remediation protected names come from the verified container-bound poller process and are compared exactly with the post-remediation process set.
- `Config.Image` is configured-reference authority; `.Image` is the actual image ID. Compose labels are not image authority.
- JSON overrides use duplicate-rejecting lexical parsing and exact byte-span deletion; unrelated bytes and decoded semantics remain unchanged.
- Poller discovery retries only the exact transient zero-match state within a monotonic deadline.
- Health executes only `docker exec hermes-bot hermes gateway status`; subprocess diagnostics are sanitized.

## Modules

- `json_override.py`: strict JSON parser and byte-span transformer
- `compose_command.py`: exact Compose recreation command and child environment
- `source_invariant.py` / `runtime_invariant.py`: source and runtime identity
- `poller_checker.py`: bounded poller convergence
- `health.py`: container-scoped gateway health
- `executor.py`: fail-closed orchestration and rollback

## Validation and usage boundary

Claim-to-test bindings are in `secret_remediation_test_evidence.json`. Linux exact-head CI must run the real Docker preflight with `PASSED` and `SKIPPED=0`; a local Windows skip is not acceptance evidence.

The entry point remains `ops.secret_remediation_r1.executor.run_remediation()`. This document does not authorize execution. Production use requires a separate explicit production task and all authority, readiness, backup, rollback, and health gates.
