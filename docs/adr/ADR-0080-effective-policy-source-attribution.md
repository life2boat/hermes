# ADR-0080: Effective Policy Resolution and Source Attribution

## Status
Accepted

## Context
As part of the Hermes Intent Control Plane (PR-1 through PR-5), tasks declare explicit `TaskIntent` contracts containing task boundaries, applicable engineering invariants, and required release gates.
Without an explicit attribution and resolution layer, agents and operators cannot deterministically trace which exact Git-committed sources establish the meaning of declared invariants and gates, nor can they verify whether all declared references resolve to existing repository contracts.

## Decision
1. Implement `ai_engineering.effective_policy` and `scripts/explain_effective_policy.py` to resolve declared `TaskIntent` references against exact Git blobs at `subject_sha`.
2. Compute deterministic cryptographic identities (`source_id`, `effective_policy_id`) over canonical JSON representations.
3. Establish fail-closed validation on both dataclass and deserialization boundaries (protecting against tampered IDs and broken bindings).
4. Restrict resolution to exact explicit matching without LLM inference, fuzzy matching, or authority expansion.

## Consequences
- **Positive**: Complete deterministic explainability of task policy and invariant/gate sources.
- **Positive**: Zero network, zero LLM calls, and zero external runtime dependencies.
- **Boundary**: Does not verify remote CI or external provider authenticity (M-PR4-001 residual boundary).
- **Safety**: Resolution cannot broaden task authority, modify database state, or bypass release gates.
