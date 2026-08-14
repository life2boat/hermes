# Effective Policy and Source Attribution Contract

Status: normative explainability contract
Schema Version: `EFFECTIVE_POLICY_SCHEMA_VERSION=1`

## Purpose

The Effective Policy resolution layer provides deterministic, offline explainability and source attribution for explicit TaskIntent declarations. It maps declared invariants, required gates, and task boundaries to their canonical repository definitions at an exact Git revision (`subject_sha`).

## Core Invariants

1. **No Authority Expansion**: Effective policy resolution is strictly an explainability layer. It cannot grant, broaden, or modify task authority (`EFFECTIVE_POLICY_EXPANDS_AUTHORITY=false`).
2. **Zero Provider / Network Dependency**: Resolution runs completely offline against exact committed Git blobs (`PROVIDER_CALLS=0`, `NETWORK_REQUIRED_FOR_CORE=false`).
3. **No LLM Policy Inference**: The engine never uses LLMs to guess or infer relevance from natural language prose (`LLM_AS_POLICY_RESOLVER=false`, `POLICY_AUTO_INFERENCE=false`).
4. **TaskIntent Integrity**: TaskIntent is immutable during resolution (`TASK_INTENT_MUTATED=false`).
5. **Deterministic Identity**: Identity (`effective_policy_id` and `source_id`) is cryptographically computed over canonical JSON representations with no timestamps or nondeterministic fields.
6. **Public Boundary Integrity**: Dataclass instances and serialized payloads are validated against tampered IDs and broken source bindings (H-PR4-001 defense).
7. **External Authenticity Residual**: PR-5 proves source attribution against the local Git tree; external artifact/CI attestation remains outside PR-5 scope (M-PR4-001 non-blocking residual).

## Precedence and Authority Sources

Source precedence follows `docs/HERMES_SOURCE_MAP.md`:
1. Task-specific `TaskIntent` (constraints, allowed/forbidden mutations, stop boundary)
2. `AGENTS.md` (repository-wide policy)
3. Canonical contract documents and modules:
   - `docs/HERMES_INVARIANTS.md` (canonical engineering invariants)
   - `docs/AGENT_RELEASE_GATES.md` & `ai_engineering/release_gate.py` (canonical release gate definitions)
   - `docs/HERMES_SOURCE_MAP.md` (precedence authority)

## Resolution Semantics

- **Invariants**: Resolved against exact headings in `docs/HERMES_INVARIANTS.md` (e.g. `R1`, `AI1`, `INV-AI-V2-001`). Unknown identifiers yield `resolution_status=UNRESOLVED`.
- **Required Gates**: Resolved against `ai_engineering.release_gate.GateName`. Unknown identifiers yield `resolution_status=UNRESOLVED`.
- **Report Status**:
  - `COMPLETE`: All declared invariants and gates are successfully resolved.
  - `INCOMPLETE`: One or more declared references cannot be resolved.

## Verification Boundaries (PR-5.1)

1. **Structural / ID Integrity (`validate_effective_policy_report`)**:
   - Proves schema shape, field types, string formats, internal hash consistency, and payload ID derivation.
   - Does **not** prove canonical policy membership, Git blob authenticity, or TaskIntent semantic origin.
   - Deserialization or structural validation alone is **untrusted** with respect to semantic claims.

2. **Authoritative Semantic Verification (`verify_effective_policy_report`)**:
   - Re-verifies report against trusted inputs: exact `TaskIntent`, exact `subject_sha`, and canonical Git blobs.
   - Deterministically re-resolves expected policy via `resolve_effective_policy()`.
   - Requires exact semantic equality across task policy attribution, policy sources, invariant resolutions, gate resolutions, unresolved references, and policy ID.
   - Fails closed on forged `RESOLVED` declarations for unknown invariants/gates, forged task policy claims, or altered source hashes.
   - No caller may treat an EffectivePolicyReport as trustworthy proof of `COMPLETE` policy resolution without passing authoritative semantic verification.
