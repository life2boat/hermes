---
title: Hermes / HealBite — Current State
version: 1.2.62
updated_at: 2026-08-18
status: active
source_of_truth: true
state_verified_against_main_sha: 185030b05033ee3779ce8f0e74f9572aa24a0640
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## Hermes / HealBite — Memory & Graph Engineering v3 PR-2 (Structural) & PR-2.1 (Integrity) & PR-2.2 (Closure)

- PR-2 foundation is COMPLETE.
- PR #192 provided the structural foundation for deterministic graph projection.
- PR #193 (PR-2.1) implemented strict read-only schema validation, exact privacy boundary checks, and projection result immutability.
- **Historical Sequencing Record for PR #193**: The pull request was merged using `--admin` before `Tests` and `Nix` workflows reached terminal state.
  - Eventual exact-head CI was PASS.
  - Reported 60 test cases was inaccurate (only ~50 were collected).
  - Canonical authoritative-source ordering and deterministic canonical source-state serialization remained incomplete.
- PR-2.2 closed the remaining pre-persistence defects:
  - Canonical authoritative-source ordering now unambiguously sorts facts by `fact_id` ASC, `current_revision` ASC, `status` ASC.
  - Immutable canonical source-state identity encoding via deterministic canonical JSON guarantees robust hash binding.
  - Projection ID derives consistently from this single source-state canonicalization rule.
  - The test suite (`tests/gateway/memory/test_graph_projection.py`) was expanded to >60 parameterized, meaningful test cases covering schema validation, sentinel-driven privacy leakage checks, and static independence.
- No graph runtime activation or mutation to production occurred.

## Hermes / HealBite — Memory & Graph Engineering v3 PR-1.4

- PR-1.4 duplicate-key fail-closed parser and final truth closure implemented.
- `json.loads` object_pairs_hook used to reject duplicate JSON keys fail-closed.
- PR #190 early-merge sequencing truth recorded.
- PR #188 historical description corrected in SOT.

## Hermes / HealBite — Memory & Graph Engineering v3 PR-1.3

- PR-1.3 is COMPLETE.
- Strict contract/schema parity closure implemented in `ai_engineering/graph_contract.py`.
- Independent module presence guard added to `tests/ai_engineering/test_contracts.py`.
- Final verified state: exact-main test suites, JSON Schema Draft202012 parity tests, and independent module guards all pass.

## Hermes / HealBite — Memory & Graph Engineering v3 PR #189 (PR-1.2)

- Forward recovery of PR #188 completed.
- Graph module and graph test suite restored from pre-corruption blobs.
- Zero-test truncation guard added.
- All exact-head CI workflows eventually PASS.
- Squash merged into main.
- Merge occurred before final Nix terminal state.

## Hermes / HealBite — Memory & Graph Engineering v3 PR #188

- Intended graph-contract closure.
- Accidental truncation of graph implementation (`ai_engineering/graph_contract.py`) and test file (`tests/ai_engineering/test_graph_contract.py`) occurred during packaging.
- Dedicated graph test file consequently collected zero graph tests and failed to protect the change.
- Exact-head Tests workflow remained FAIL because the TUI notification poller test failed (`3 != 2`).
- PR #188 was nevertheless merged using admin bypass.
- PR #189 restored the graph implementation/tests and introduced the independent non-truncation guard.

## Hermes / HealBite — Memory & Graph Engineering v3 PR-1.1

- PR-1.1 is COMPLETE.
- Hardened Graph Contracts implemented in `ai_engineering/graph_contract.py`.
- Established `GRAPH_NODE_IDENTITY_SEMANTICS=ENTITY` where GraphNode represents a semantic entity.
- Established `REJECT_CONFLICT` assertion behavior for duplicate logical relations.
- Enforced boundaries: max 1000 nodes, 5000 edges, secret rejection, and length bounds.
- Established explicit invalidation and classification (`CURRENT`, `STALE`, `DELETED_SOURCE`, `UNKNOWN_SOURCE`).
- Wrote JSON Schema for offline verification: `schemas/memory-graph-contract-v1.schema.json`.
- Implemented and passed all adversarial graph contract tests.
- Removed accidental artifact `architecture.json`.
- Confirmed PR #187 exact-head workflows successfully completed.
- No production execution or mutation occurred.

## Hermes / HealBite — Memory & Graph Engineering v3 PR-1

- PR-1 is COMPLETE.
- Established `ai_engineering/graph_contract.py` containing deterministic graph node/edge definitions.
- Defined `GraphProvenance` to enforce authoritative binding to SQLite `memory_os_facts`.
- Wrote ADR 0081 defining strict boundaries: SQLite is AUTHORITATIVE, Graph is DERIVED_REBUILDABLE.
- No LLM provider is trusted to generate graph edges without provenance.
- Deterministic IDs and tamper detection implemented and tested against adversarial conditions.
- No production database mutations or Qdrant changes occurred.

## Hermes C1.1 — Nix CI Truthfulness & Merge-Sequencing Hardening

- C1.1 is COMPLETE.
- Hardened `.github/workflows/nix.yml` so that non-technical PR sticky-comment cleanup failures do not cause a workflow FAIL.
- Real Nix failures and hash diagnostics remain hard failures.
- No production execution or mutation occurred.

## ProductionRuntimeAttestation C1 — Production Readiness Evidence Bridge v1

- C1 is COMPLETE.
- `ai_engineering/production_readiness_evidence.py`: deterministic offline verifier.
- `schemas/production-readiness-evidence-v1.schema.json`: versioned JSON schema.
- `scripts/check_production_readiness_evidence.py`: offline CLI.
- `tests/test_production_readiness_evidence.py`: 27/27 mandatory tests passing.
- evidence pipeline: Attestation → Comparison → EvidenceReceipt → GateEvidence(PRODUCTION_READINESS_GATE).
- MATCH alone is not sufficient; post-health PASS + freshness + identity are required.
- Historical B2 synthetic regression confirmed: MATCH + INSUFFICIENT_EVIDENCE → BLOCKED.
- `EVIDENCE_EXPANDS_AUTHORITY=false`.
- no production access; no live collection; repository-only.

## ProductionRuntimeAttestation v1 — B1+B2 Source-of-Truth Closure

- B1 is complete.
- B2 is complete.
- PR #183 is merged.
- canonical repository main at task start was `2c60edcd691a161387b2ef11479cec43cd20520d`.
- ProductionRuntimeAttestation v1 is available.
- read-only live collectors are available.
- authoritative attestation exists.
- production structural comparison was MATCH.
- post-collection historical health proof is INSUFFICIENT_EVIDENCE.
- no production mutation occurred.
- production_sha remains unknown.
- historical execution provenance remains UNPROVEN.
- no B3 remediation is required by the MATCH result.

## HealBite Secret Remediation R1 — Clean Forward Source Closure

- Repository closure base: `e85ca7dbee2025320c5daf61181a6c1142f18a9b`.
- Forensic reference: `f438671ee445ae5a73a2aad235298fe5f1439536`.
- `PRODUCTION_FORENSIC_RUNTIME_STATE=PASS`.
- `HISTORICAL_EXECUTION_PROVENANCE=UNPROVEN`.
- `PRODUCTION_REPAIR_REQUIRED=false`.
- Independently hardened: child-only Compose interpolation, verified-process protected-name capture, bounded poller convergence, container-scoped health, configured image-reference authority, `--no-interpolate` source validation, and strict JSON byte-span removal.
- Whole-document JSON reserialization and forensic-branch provenance claims are rejected.
- Repository-only change: production, database, Qdrant, secrets, containers, and deployment remain unchanged.

## Hermes Intent Control Plane v1.2 — Evidence Semantics Hardening

- Hermes Intent Control Plane v1.2 Evidence Semantics Hardening is COMPLETE.
- Status: `HERMES_INTENT_CONTROL_PLANE_V1_2=EVIDENCE_SEMANTICS_HARDENED`.
- All requirements from HEALBITE-PROD-R02A-ICP-001 are fulfilled:
  - **Artifact Byte Verifier**: Implemented `ai_engineering/evidence_verifier.py` providing fail-closed byte-binding for artifacts (`ArtifactVerificationError`).
  - **Evidence Sufficiency Review**: Implemented `ai_engineering/sufficiency_review.py` for semantic adjudication, ensuring `INDEPENDENT_AGENT` is distinct from `HUMAN` and cannot authorize critical production behavior alone (AI6 compliance).
  - **Receipt Schemas**: Implemented `ExecutionAuthorityReceipt` and `EvidenceCollectionReceipt` with deterministic content hashing.
  - **Convergence v2**: Implemented `ai_engineering/convergence_v2.py`. `HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION` tasks with `PASS` evidence but no sufficiency review evaluate to `EVIDENCED_UNREVIEWED` and do not converge. V1 backwards compatibility maintained.
- **Dogfood #1 Replay (Offline)**:
  - Adjudication and offline replay docs successfully completed and checked into `docs/dogfood`.
  - Replay confirmed that missing sufficiency review on Dogfood #1 (AC4/AC9 gaps) causes fail-closed `NOT_CONVERGED` via Convergence v2.
  - No new runtime evidence was collected, and no production/WSL systems were accessed.
- Validated via 100% green test suite (466 tests passed).

## Hermes Intent Control Plane — Operational Hardening v1.1

- Hermes Intent Control Plane Operational Hardening v1.1 is COMPLETE_AND_MERGED (PR #177, commit `60b403723dcdc49261f3ff99ae580d64e747cb00`).
- Status: `HERMES_INTENT_CONTROL_PLANE_V1_1=OPERATIONAL_HARDENED`.
- All operational dogfood findings (CPF-001 through CPF-007) are resolved, verified, and merged:
  - **CPF-001 (CLI import defect)**: Fixed `deserialize_intent` imports across `scripts/clarify_task.py` and `scripts/requirements_gate.py` (`CPF_001=CLOSED`).
  - **CPF-002 (Runtime authority separation)**: Clarified and enforced that TaskIntent does not encode runtime execution authority (`CPF_002=CONFIRMED_BY_DESIGN`).
  - **CPF-003 (Requirements review identity binding)**: Bound `RequirementsQualityReview.review_id` deterministically via `compute_requirements_review_id()` and public factory `create_requirements_quality_review()` with tamper validation (`CPF_003=CLOSED`).
  - **CPF-004 (Public Evidence API)**: Exposed public constructors `compute_observation_id()`, `create_evidence_observation()`, `compute_bundle_id()`, `create_evidence_bundle()` in `ai_engineering` with deterministic ID hashing (`CPF_004=CLOSED`).
  - **CPF-005 (Convergence CLI deserialization)**: Enabled `scripts/converge_task.py` to accept raw str/bytes and execute deterministically in subprocesses (`CPF_005=CLOSED`).
  - **CPF-006 (Lineage contract clarification)**: Documented Lineage v1 graph contract where TaskIntent owns criteria directly and edge relations connect `TASK -> IMPLEMENTS -> CRITERION` and `EVIDENCE -> VERIFIES -> CRITERION` (`CPF_006=CLOSED`).
  - **CPF-007 (Clarification integrity hardening)**: Hardened `ClarificationReport` validation with `compute_clarification_id()` and structural question-count/ready-flag consistency (`CPF_007=CLOSED`).
  - **CPF-008 (CLI direct-script repository-root import portability)**: Guaranteed repository root on `sys.path` across all CLI scripts (`prepare_task.py`, `clarify_task.py`, `requirements_gate.py`, `analyze_task.py`, `converge_task.py`, `explain_effective_policy.py`) for deterministic cross-platform direct execution (`CPF_008=CLOSED`).
  - **Full E2E Integration Suite**: Added full offline subprocess integration test suite in `tests/ai_engineering/test_intent_control_plane_integration.py`.
- **Dogfood Revision-2 Requalification (`HEALBITE-PROD-R02A-ICP-001`)**:
  - Full offline pipeline executed against `60b403723dcdc49261f3ff99ae580d64e747cb00` with parent intent `ad3de22f18e21846c248768ecb35cccaf2570747126324a954ab5882ca29b5a8`.
  - All offline stages (prepare, clarify, requirements gate, lineage analysis, effective policy, semantic verification) passed deterministically (`DOGFOOD_R2_REQUALIFICATION=PASS`).
  - `RUNTIME_AUTHORIZATION_STATUS=READY_FOR_OPERATOR_AUTHORIZATION` (execution stopped strictly before WSL/production access).
- Production, database, Qdrant, secrets, and live deployment remain untouched.
- All planned capabilities are implemented, verified, and adopted into canonical workflow:
  - **TaskIntent / TaskLineage**: Schema version 1, exact base-SHA binding, canonical serialization, and lineage DAG relations.
  - **Cross-Artifact Analyzer**: Deterministic offline semantic and structural consistency verification.
  - **Clarify & Requirements Quality Gate**: Non-mutating clarification and formal criteria gating (`RequirementsGateReport`).
  - **Evidence-Bound Convergence**: Deterministic evaluation of acceptance criteria and release gate evidence against exact TaskIntent and subject SHA (`ConvergenceReport`).
  - **EvidenceBundle Public Integrity (PR-4.1)**: Fail-closed tampering defense on public API boundary (`H_PR4_001=CLOSED`).
  - **Effective Policy / Source Attribution (PR-5 & PR-5.1)**: Deterministic resolution of applicable invariants and required release gates to exact Git blobs (`EffectivePolicyReport`). PR-5.1 closed `H_V1_PR5_001` via `verify_effective_policy_report()`, enforcing authoritative semantic re-verification against trusted `TaskIntent`, `subject_sha`, and canonical Git blobs (`H_V1_PR5_001=CLOSED`). Structural validation alone is untrusted for semantic resolution.
  - **Lifecycle & Template Adoption**: Fully integrated into `docs/TASK_LIFECYCLE.md`, `docs/TASK_TEMPLATE.md`, `AGENTS.md`, and `docs/HERMES_SOURCE_MAP.md`.
- Residual limitations:
  - `M_PR4_001=KNOWN_NON_BLOCKING_EXTERNAL_AUTHENTICITY_LIMITATION` (external authenticity of artifact refs/digests is bounded to Git-committed objects and local execution).
  - `M_V1_PR5_001=NON_BLOCKING_KNOWN_BOUNDARY` (remote CI/external provider signing remains out of scope).
  - `M_V1_ADOPTION_001=ACCEPTED_NON_BLOCKING_DESIGN_BOUNDARY` (lifecycle adopted as engineering contract/workflow; runtime orchestrator not implemented).
- Non-negotiable architectural boundaries:
  - `PROVIDER_CALLS=0`
  - `NETWORK_REQUIRED_FOR_CORE=false`
  - `LLM_AS_JUDGE=false`
  - `LLM_AS_POLICY_RESOLVER=false`
  - `EFFECTIVE_POLICY_EXPANDS_AUTHORITY=false`
- The Intent Control Plane explicitly does NOT:
  - Grant production authority or bypass release gates.
  - Authenticate remote CI artifacts or external providers.
  - Authenticate human reviewers.
  - Replace Agent Release Gate or production readiness verification.
- Production, database, Qdrant, secrets, and deployment remain unchanged.

## Hermes Intent Control Plane PR-1

- `TaskIntent` schema version 1 and `TaskLineage` schema version 1 are implemented and merged (PR #165, with corrective restoration in PR #166).
- `scripts/prepare_task.py` integrates `--intent` to enforce exact base-SHA binding and canonical task intent validation.
- The `TaskIntent` contract supports canonical serialization, deterministic digests, revision chains, and strict lineage relation validation.
- Raw prompt and chat storage remain forbidden; this architecture relies on operator intent bounding.
- PR #166 repaired the accidental truncation of `prepare_task.py` during intermediate local testing; the final canonical main contains the intended correct source.
- Production, database, Qdrant, secrets, deployment, and providers remain unchanged.
- PR-4 Evidence-Bound Convergence is IMPLEMENTED.
- PR #171 merged it.
- EvidenceBundle schema v1 is implemented.
- ConvergenceReport schema v1 is implemented.
- Convergence recomputes canonical PR-3 Requirements Gate and PR-2 analysis.
- Evidence is bound to exact intent / analysis / subject SHA.
- Acceptance-criterion and required-gate evidence are evaluated deterministically.
- FAIL evidence dominates contradictory PASS evidence.
- LLM_AS_JUDGE=false.
- LLM_SELF_DECLARED_CONVERGENCE=false.
- PROVIDER_CALLS=0.
- NETWORK_REQUIRED=false.
- CONVERGENCE_MUTATES_INPUTS=false.
- Convergence does NOT authorize production or deployment.
- PR-5 Effective Policy / Source Attribution is IMPLEMENTED (PR #174).

## Hermes Intent Control Plane PR-4.1

- PR-4.1 closed H-PR4-001 (independent Manus adversarial audit, SEVERITY=HIGH). Merged in PR #172.
- `evaluate_convergence` now validates `EvidenceBundle` at its public boundary via `validate_evidence_bundle()`.
- Direct dataclass construction cannot bypass `observation_id` integrity validation.
- Direct dataclass construction cannot bypass `bundle_id` integrity validation.
- Self-inconsistent evidence (tampered IDs) fails closed with `ConvergenceError`.
- Self-consistent evidence bound to the wrong context (wrong task / intent / analysis / subject SHA) remains `NOT_CONVERGED` — not converted to an integrity error.
- `validate_evidence_bundle()` is a single canonical validator: both dataclass and mapping/JSON inputs go through the same `_validate_bundle_from_mapping()` path. No duplicate integrity algorithm exists.
- H-PR4-001 is CLOSED. `FALSE_CONVERGENCE_REPRODUCTION=BLOCKED`.
- M-PR4-001 remains a non-blocking limitation: external authenticity of `artifact_ref` / `artifact_digest` / `producer_id` is NOT independently verified by PR-4 or PR-4.1.
- PROVIDER_CALLS=0. NETWORK_REQUIRED=false. CONVERGENCE_MUTATES_INPUTS=false.
- Production, database, Qdrant, secrets, and deployment remain unchanged.

## Hermes Intent Control Plane PR-3

- The PR-3 Clarify + Requirements Quality Gate was merged in PR #170.
- Clarification checks ensure the TaskIntent has no unresolved blocking unknowns (`clarify_task.py`).
- Requirements Gate (`requirements_gate.py`) verifies the intent digest and task revision against the intent, ensuring the intent is READY and produces a formal `RequirementsGateReport`.
- A passing PR-3 gate sets `REQUIREMENTS_READY_FOR_DOWNSTREAM_ENGINEERING=true`.
- The logic remains deterministic, offline (PROVIDER_CALLS=0), and preserves invariant `CLARIFY_MUTATES_TASK_INTENT=false`.

## Hermes Intent Control Plane PR-2

- The deterministic, offline, read-only Cross-Artifact Analyzer (`scripts/analyze_task.py`) was merged in PR #168.
- It detects ORPHAN_ACCEPTANCE_CRITERION (scoped identity only), ORPHAN_EXECUTION_TASK, ORPHAN_EVIDENCE, SOURCE_IDENTITY_MISMATCH (when an independent expected_base_sha is supplied), and TASK_IDENTITY_INCONSISTENCY.
- The `analysis_id` is a strict, canonical payload digest linking the validated intent, lineage digest (canonically graph-sorted), expected_base_sha, and full finding semantics.
- Path-based mutation boundary analysis and gate coverage analysis are explicitly DEFERRED due to missing canonical mappings in the v1 lineage schema.
- AnalysisReport v1 schema enforcement and `expected_base_sha` strict-typing and hard-link alias protections are merged via PR #169.

## Memory Convergence v1.1 repository state

- SQLite remains authoritative and Qdrant remains derived and untrusted.
- When `MEMORY_VECTOR_ENABLED=true`, the gateway owns one bounded task per
  process: immediate startup recovery plus periodic bounded ticks. Disabled
  mode creates no runtime database or Qdrant side effect.
- Runtime status publishes aggregate convergence/alert state and reconciliation
  timestamps without fact content, vectors, payloads, identifiers or secrets.
- BLOCKED repair requires one owner and explicit bounded outbox operation ids.
- Historical orphan classification is offline/read-only; live scan and delete
  are not performed or authorized.
- The canonical staged migration registry now includes one ordered
  `memory_convergence` component backed by the shared stdlib-only
  `gateway/memory/schema.py` contract. Production startup validates this schema
  read-only; safe development/test initialization may still create it.
- This is repository implementation and acceptance, not proof of production
  migration, activation or live reconciliation.

## 1. Summary

- The repository now contains a thin Google Antigravity workspace adapter:
  an Always-On-intent core rule, a task-bootstrap skill, and a read-only
  onboarding prompt. These files point back to `AGENTS.md`, current state,
  lifecycle, invariants, ADRs, and `scripts/prepare_task.py`; they do not create
  a competing source of truth or grant production/provider mutation authority.
- Antigravity is the intended primary day-to-day executor, Codex is the reserve
  executor after an explicit ownership handoff, and Manus remains read-only
  research and assurance. Only one executor may mutate a branch or worktree.

- The Prompt Engineering System version `1`, merged by PR #147, extends the stdlib-only
  engineering-control layer. It provides typed `PromptSpec`, deterministic tagged/
  Markdown compilation, relevant/current/authoritative context selection, untrusted-
  input isolation, structured validator/linter diagnostics, model-capability checks,
  versioned prompt provenance, and a provider-free eight-case regression corpus.
- Behaviour Trace schema version `2` adds closed prompt provenance while retaining
  schema-v1 replay compatibility. Traces store no compiled prompt, dynamic payload,
  raw chain-of-thought, secret, credential, raw provider response, or private message.
- The prompt-quality corpus is technically `PASS` at digest
  `d52adea60862ad5ca2b71a23dfd506adc02ca8dcb3b6270ab79a51bc949c86ea`,
  but remains lifecycle state `CANDIDATE`; human review is `NOT_PERFORMED`. The
  canonical exact-head Agent Release Gate workflow runs this provider-free corpus as a separate
  technical step. Neither prompt quality nor trace metadata grants provider or
  production authority.

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Project state in this document was verified against HealBite main SHA:
  `e102b64dcfdea0120c891ba1298fecfd6c75cb62`.
- This verification SHA records repository state and Source-of-Truth docs closure
  only; it does not identify a deployed production revision.
- PR #126 merged the Phase 0 AI-engineering foundation into canonical main.
- A Phase 0.5 cold-start usability check reconstructed the architecture,
  forbidden changes, deployment gates and new-task workflow solely from the
  five foundation documents; result: `PASS`.
- PR #127 merged five populated architecture decisions and the structured
  `knowledge/` layer for architecture, decisions, failures, patterns,
  operations and AI-agent lessons into canonical main.
- PR #128 merged the Phase 2 execution layer: AI change-review and production-
  readiness checklists, mandatory pre-task context preparation in the task
  template, and `scripts/prepare_task.py` with focused tests.
- PR #129 merged the operational-adoption layer: mandatory prepared context in
  `AGENTS.md`, the task lifecycle, and the sanitized failure-capture loop.
- PR #139 established the Hermes AI Engineering System v2 architecture and
  contract foundation. It
  defines agent behaviour, deterministic-first behaviour evaluation, LLM Ops
  policy, distinct merge and production-release gates, and the
  Skill-to-Loop-to-Graph maturity lifecycle.
- ADR-0074 through ADR-0077 record the corresponding durable decisions,
  including candidate-only governed agent improvement.
- The stdlib-only ai_engineering package implements schema version 1
  behaviour traces, compatible scenario schema v1 reads, explicit scenario
  schema v2, recursive evidence sanitization, canonical JSON/SHA-256 identity,
  safe fixture loading, and deterministic provider-free replay.
- Deterministic graders, a closed assertion registry, offline eval runner,
  stable machine reports, and digest-bound baseline comparison are implemented.
  The 49-case corpus is GOLDEN following independent human review bound to
  dataset version `agent-behaviour-v1`, engine version `1`, approved candidate
  head `fa77b12cb9a0f1b1e8b0eaa596cd41092fdfdb20`, and corpus digest
  `e2580fb10c6d02a55ace0efc9092bd6f3092a9a3a188515c5dba32b44708c8c7`.
- Model policy version `1` implements the closed seven-class engineering task
  matrix, reasoning requirements, explicit substitution classes, provider-
  change evidence, sanitized receipts, and the invariant that model capability
  never expands authority.
- Cost policy version `1` implements explicit call/token/cost budgets, complete
  primary/retry/judge/fallback/live-eval accounting, deterministic decimal
  estimation, externally supplied rate-card schema version `1`, canonical
  rate-card SHA-256 identity, currency checks, and fail-closed required unknown
  evidence. `UNKNOWN` cost never becomes zero.
- `scripts/check_llm_ops_policy.py` exposes these contracts offline with stable
  JSON and distinct PASS/FAIL/BLOCKED exit codes. It performs no provider or
  pricing lookup. The combined `LLMOpsReceipt` is not a release gate.
- Release gate schema/policy version `1` implements closed `MERGE` and
  `PRODUCTION_RELEASE` targets, deterministic sensitivity-derived requirements,
  independent fixed-schema gate evidence, exact source binding, technical
  blocker/governance separation, canonical receipts, and stable
  PASS/FAIL/BLOCKED CLI exits.
- The read-only `Agent Release Gate` pull-request workflow evaluates the exact
  PR head. Its conservative merge profile independently runs code, GOLDEN
  offline behaviour, secret-scan, and adversarial evidence. It reports cost,
  live behaviour, and production readiness as optional `NOT_PERFORMED`; a
  merge PASS never becomes a production-release PASS.
- PR #144 merged PR-6 into canonical main. It implements deterministic
  Failure-to-Eval candidate construction and procedure-maturity receipts. Both
  remain offline, review-only evidence: they cannot mutate the Golden corpus,
  self-promote a candidate, compile a graph, expand authority, or change a
  runtime or production system.
- `HERMES_AI_ENGINEERING_SYSTEM_V2=COMPLETE`: the v2 repository-contract
  foundation is complete. It does not authorize autonomous production actions,
  Golden self-promotion, graph compilation, auto-deploy, or unbounded
  self-improvement.
- This repository-only update changes no product runtime, provider route,
  database, Qdrant state, secret, feature flag, container, or production state.
- The repository Qwen Vision integration remains opt-in and provider-scoped:
  canonical `alibaba`/`qwen-dashscope` selects DashScope with an explicit model,
  `qwen-oauth` remains a distinct Portal identity, and ambiguous bare `qwen`
  fails closed. No default Qwen vision model or production activation is added.
- The uploaded still-image `vision_analyze_tool` Qwen/DashScope route keeps the
  strict one-external-request policy. Its failure becomes a sanitized
  user-safe failure and does not silently forward pixels to another provider.
  Browser screenshot and video tools retain their separately bounded capture,
  resize, and multi-frame semantics.
- Durable SQLite sessions, JSON snapshots, context compression, trajectories,
  background review, and Memory OS inputs receive a text-only sanitized copy
  of multimodal messages. Inbound Telegram image files are session/actor-bound,
  private-mode cached, and removed after their final consumer, failure,
  cancellation, or abandoned-batch cleanup.
- Prepared task context binds Git SHA, branch, changed paths, tracked core docs
  and ADRs; pytest cache is classified as non-authoritative and cannot prove a
  test PASS.
- The Knowledge Pack remains documentation-only and introduces no runtime,
  database, container, Qdrant or production behavior.
- The local `origin` remote points to upstream `NousResearch/hermes-agent` and
  is not the HealBite project remote.
- Canonical checkout: `/home/hermes/.hermes/hermes-agent`.
- Canonical checkout state during this update: dirty; it must not be cleaned,
  reset, stashed or modified by unrelated tasks.
- Repository main defines a manual, main-only, GitHub-hosted exact-tree image
  build path that publishes an immutable SHA tag and digest to
  `ghcr.io/life2boat/hermes`.
- The remote-build workflow has read-only repository access plus package
  publication access. It is not a deployment workflow and does not authorize a
  production image pull, container recreate, configuration change, or DB write.
- The repository remediation candidate binds 29 image-secret exceptions to
  exact paths, rules, package versions, artifact identities, and file hashes;
  it also removes 16 dependency artifacts before they enter recoverable final
  image layers and rewrites five scanner self-detections without weakening
  runtime detection.
- A zero-finding OCI result for that candidate is NOT CONFIRMED until a new
  post-merge exact-main image build and full image-secret scan complete.
- Read-only status check on 2026-08-07 found the `hermes-bot` container
  stopped. This implementation task did not start, rebuild, restart or recreate it.
- Last confirmed Qdrant runtime: running, restart count 0.
- Qdrant has not been intentionally changed by recent HealBite rollout steps.
- Production git SHA: `unknown`.
- Production image digest remains unmapped to a source SHA by this document.
- Next-generation Qwen benchmark evidence now spans two bounded external tasks:
  Q1 access/schema plus `qwen3.7-plus` quality evidence anchored to repository
  main `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`, and the completed
  `qwen3.6-plus` three-image quality benchmark anchored to repository main
  `f45a3c16b49282775d06003948e449d756aa54f2`.
- Both benchmark tasks used repository food-vision prompt, validator, local
  confirmation derivation, manifest, and scoring together with an approved
  task-scoped DashScope OpenAI-compatible external harness.
- Neither benchmark task validated the current built-in Hermes Qwen OAuth
  runtime; `qwen3.7-plus` and `qwen3.6-plus` benchmark validity does not prove
  deployable Qwen runtime integration.
- Benchmark assets remained the same three operator-approved sanitized images
  with SHA256 `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Current production vision routing remains on the existing Gemini deployment
  state only; this docs task did not change production config or runtime, and
  the benchmark does not endorse Gemini as a winner.
- Qwen vision code remains present in main, but Qwen is not deployed or active
  in production after the rejected live activation.
- A component-grounded Stage-1 food vision contract is implemented in repository
  code and has passed provider-limited offline validation only.
- Stage-1 vision output rejects model-generated aggregate calories/macros and
  cannot stage a diary-ready pending meal directly from a photo result.
- Mixed-plate photo flow uses a two-step component confirmation path: Stage-1
  confirms visible components first, Stage-2 calculates nutrition only from
  confirmed components and then asks for the final diary save decision.
- User correction commands for meal-photo components are implemented locally
  (confirm/cancel/replace/add/remove/weight) without generic-agent handoff.
- Offline mixed-plate food-vision quality fixtures and deterministic thresholds
  are present in the test suite.
- A canonical non-production provider-executing quality harness now exists at
  `scripts/run_food_vision_quality.py`, bound to immutable synthetic
  `food_vision_quality_v1`/`v2`, candidate `food_vision_quality_v3`, and
  sanitized receipt-v3 evidence. Qwen rollout eligibility remains blocked and
  no model is approved.
- The Stage-1 food-vision prompt remains shorter, provider-neutral, and no
  longer anchored to the failed benchmark plate or pastry labels.
- Local confirmation requirement is derived deterministically from validated
  inventory data and cannot be suppressed by provider `needs_user_confirmation=false`.
- Mixed plates, sauces, low confidence, uncertainty, warnings, missing weights,
  broad ranges, and over-specific normalization force clarification locally.
- Next-generation Qwen request accounting remained within approved budgets:
  Q1 used 6 provider requests total (3 access probes, 3 `qwen3.7-plus`
  benchmark requests), and the `qwen3.6-plus` benchmark used exactly 3 provider
  requests with 0 access probes, 0 retries, 0 fallbacks, 0 repair requests,
  0 Telegram requests, 0 production DB opens, 0 production DB writes, and
  0 Qdrant requests.
- All three next-generation aliases `qwen3.7-plus`, `qwen3.6-plus`, and
  `qwen3.6-flash` were operationally reachable on the access asset within that
  task-scoped external benchmark context, each produced schema-valid inventory
  output, and each passed the local validator.
- Access/schema success in that external benchmark context is not a
  food-quality benchmark and does not make any tested alias rollout eligible.
- `qwen3.7-plus` completed the earlier three-image benchmark and remained
  `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE` with major-component precision
  `0.111111`, major-component recall `0.444444`, sauce recall `0.5`,
  confirmation correctness `1.000`, ambiguity gate pass `true`, aggregate
  nutrition violations `0`, and invalid staging `0`.
- `qwen3.6-plus` has now completed its own three-image benchmark and remained
  `QWEN36_PLUS_FAIL_CLOSED_COMPATIBLE` with major-component precision
  `0.222222`, major-component recall `0.555556`, sauce recall `0.5`,
  confirmation correctness `1.000`, ambiguity gate pass `true`, aggregate
  nutrition violations `0`, and invalid staging `0`.
- Historical `qwen3.6-flash` quality evidence was previously recorded as a
  3-request benchmark with precision `0.7777777777777778`, recall
  `0.7777777777777778`, and sauce recall `0.3333333333333333`; its exact
  receipt is unavailable, so its diagnostics cannot be reconstructed.
- A replacement `qwen3.6-flash` run on canonical main
  `caadf124d006a543af012ac2b9b42343fc7524d0`, using manifest SHA256
  `46eeef07535bf814167e2dab8c8c700ff4de14e1d47ecf7f8cfab21f6f3896c3`,
  used exactly 3 provider requests, 0 retries, and 0 cross-provider fallbacks.
  Its durable private receipt SHA256 is
  `7b6c07a2912237bf353407ff3806560bce5b1b5ebd54b9f40b362f96f00efdc6`;
  aggregate precision was `1.0`, recall `0.3333333333333333`, sauce recall
  `0.0`, unsafe aggregate count `0`, and invalid aggregate count `2`.
- The replacement quality gate failed: fixture B matched three normalized
  components, while fixtures A and C were schema-invalid. This replacement is
  new evidence and does not reconstruct the lost historical receipt;
  `qwen3.6-flash` remains not rollout-eligible.
- The `qwen3.6-plus` benchmark improved aggregate precision and recall versus
  `qwen3.7-plus`, but both remained below the quality gate and neither became a
  benchmark candidate.
- Relative to the previous `qwen3-vl-8b-instruct` benchmark, `qwen3.6-plus`
  aggregate precision, recall, and sauce recall regressed, while schema safety
  remained valid and ambiguity handling remained passing.
- The simple-plate sample produced `0.0` major-component precision and `0.0`
  major-component recall for `qwen3.6-plus`; this is a confirmed benchmark
  outcome, not a confirmed root cause.
- Provider-free forensics verified the exact private receipt digest and
  sanitization. The benchmark and runtime both use
  food_vision_inventory_v1, the same _VISION_PROMPT, the same
  validate_food_vision_inventory validator, and fail-closed local confirmation
  semantics; benchmark/runtime schema parity is PASS.
- Historical receipt schema version 2 retained only SCHEMA_INVALID for fixtures
  A and C and did not retain the local validator reason. Their exact trigger
  cannot be reconstructed, so schema-invalid observability for that immutable
  run is INSUFFICIENT; no visual-recognition failure is inferred.
- Fixture A is a low-complexity separated apple/banana/bread control, so the
  smallest proven failure class is schema nonconformance. Fixture C is a
  product-relevant condiment scene, but its unlabelled white sauce is visually
  ambiguous between sour cream and similar white condiments. Exact sour-cream
  scoring is therefore too specific for the runtime generic-label plus
  clarification policy; benchmark v2 requires a new immutable successor rather
  than mutation.
- Receipt schema version 3 records only the validator closed reason code and a
  coarse static trigger class. It never records provider output, prompts, image
  data, identifiers, paths, credentials, or request payloads. Historical
  version-2 evidence remains unchanged.
- Product-aligned `food_vision_quality_v3` is a provider-neutral `CANDIDATE`
  successor at manifest SHA256
  `543948ff57e27327ec1233a282a62fb230d39b12c02cde0e63e96955500e4202`.
  PR #153 merged this immutable candidate contract into canonical main at
  `57b4376464d0d40926320ced73e5d4b601dea86e`.
  It reuses all three exact v2 PNG identities without copying or changing them.
  A/B preserve recognition and distractor controls. C requires ketchup,
  generic yellow sauce, and generic `sauce` plus clarification; plausible exact
  white-condiment subtypes are unsupported specificity, not schema invalidity.
- The v3 review package SHA256 is
  `9d67211c005ad5b7758e67ce6f58c8c5d5a29d6739039f463dc2cb2d9c7762a1`.
  A human operator reviewed those exact canonical Git bytes and all three exact
  fixture hashes on 2026-08-13. The sanitized immutable human-review receipt at
  `tests/fixtures/food_vision_quality/v3/human-review.json` has SHA256
  `5b353d37be2ca9ebb6f7c54909ca4f049e6c9982bb2a19045c97ec7f6fecd12d`;
  overall visual review is `PASS`. Fixture C confirms that the white sauce's
  exact subtype is not visually provable and generic `sauce` plus clarification
  is correct. Reviewer provenance is role-only (`HUMAN_OPERATOR`), with no
  personal identity recorded. Lifecycle remains `CANDIDATE`: reference truth is
  reviewed, but no provider model is evaluated or approved. No Fixture D was
  added because existing fixtures cover the required evidence.
- V3 keeps runtime schema `food_vision_inventory_v1`, request budget 3, retries
  0, cross-provider fallback 0, receipt schema version 3, and all existing
  quality thresholds. Provider execution remains outside this repository task.
- The three-image benchmark remains a release gate only and is too small to
  establish general superiority or inferiority of one model over another.
- No provider is eligible for rollout, automatic provider selection remains
  false, deployment remains unauthorized, and deployment remains blocked.
- Weekly/shopping production feature flags: last confirmed target state is
  feature-disabled for shopping and allowlisted for weekly, but effective
  runtime config must be re-confirmed before any new rollout decision.
- PR #115 merged the additive fridge-menu schema (`user_inventory`,
  `weekly_menu_plans`, `planned_meals`, `planned_ingredients`) and the strict,
  cache-stable weekly JSON prompt builder into repository main.
- The follow-up repository implementation adds a Telegram FSM for text or photo
  inventory input, strict 7-day/3-meal validation, an HTML menu plus a separate
  shopping-list block, and explicit save/regenerate/cancel callbacks.
- Fridge-menu inline state-changing callbacks are session-bound: stale same-user,
  cross-user, duplicate, and post-restart callbacks fail closed without SQLite
  mutation. Textual `/cancel` retains its owner-local current-session behavior.
- The fridge-menu schema has not been applied to production and the Telegram UI
  has not been deployed or manually smoke-tested.
- Repository-local procedural entry points are being introduced under
  `skills/deploy/`, `skills/memory/`, and `skills/telegram/`; passive policy
  files point to those procedures instead of duplicating operational steps.
- The staged-migration gate now exposes one canonical authority-package producer:
  operation-specific v3 initial approval/policy documents, exact plan v8,
  plan-dependent companions, final-authority v2, and read-only package validation.
  The full canonical registry is preserved while the operator-bound expected
  mutation subset must equal the read-only derived effective subset at plan time
  and immediately before DDL.
  Operator authorization and external P5B/P6A evidence remain explicit inputs;
  producer output alone never authorizes a production mutation.
- A separate one-time legacy provenance bootstrap contract can transition an
  exact legacy image with an unknown OCI revision to a provenance-valid exact-main
  image only after the private exact-image rollback archive passes structural
  verification and Docker load rehearsal bound to the exact plan. Ordinary
  deploy still rejects every missing or invalid baseline revision.
- No production authority package, runtime attestation, backup, migration, image
  build, deploy, secret change, container change, SQLite write, or Qdrant mutation
  was performed while adding this repository tooling.
- This documentation refactor does not build, deploy, restart, change
  production configuration, write SQLite, or mutate Qdrant.
- The Phase 0 engineering foundation now defines a repository-grounded source
  map, system model, invariant index, AI-agent decision rulebook, and reusable
  task template under `docs/`. These documents link to existing executable
  contracts and do not introduce a new runtime service or production topology.
- `Knowledge Pack` currently names this versioned engineering-document set; a
  separately executable Knowledge Pack component is not implemented or deployed.

## 2. Stable Capabilities

- Telegram text flow is stable for ordinary user turns.
- Telegram onboarding and profile flows are stable.
- Profile macro targets and calorie targets are stored in SQLite.
- Diary and statistics commands are stable.
- Meal photo confirmation/cancel state is fail-safe: a meal is not saved before
  explicit confirmation.
- Photo routing is constrained away from terminal, code execution and filesystem
  tools.
- Tool gating is enforced before model invocation for Telegram turns.
- Dangerous tool schemas are not exposed to ordinary Telegram users.
- Provider errors must remain masked from users.
- HealBite uses SQLite as the source of truth for product data.
- Memory OS treats SQLite as source of truth and Qdrant as rebuildable index.
- Memory-vector convergence is implemented in the repository with an atomic
  SQLite fact/outbox transaction, revisioned owner-scoped `UPSERT`/`DELETE`,
  bounded at-least-once reconciliation, strong Qdrant acknowledgement, durable
  retry/block state and privacy-safe aggregate health. This is repository state,
  not proof of production migration, activation or live Qdrant reconciliation.
- User isolation and fail-closed cleanup are project invariants.
- Weight and water tracking have passed previous controlled production smokes.
- Weekly menu backend mutation and validated draft generation were merged in PR43.
- Weekly menu backend merge commit:
  `31f2594d2de352db3c0c6c78513770bdf5c606ab`.
- Fridge-to-menu data-layer and prompt contracts were merged in PR #115 with
  merge commit `492b50e979770ed5004bd6e025b9b0642636030a`.
- The repository Telegram flow accepts text or one refrigerator photo, parses
  Vision text locally, validates a complete 7x3 menu, renders HTML with a
  separate shopping list, and persists only after explicit save.
- Focused regressions cover generation, retry-limit, Vision, and storage failures;
  failed storage rolls back fully, preserves user isolation, and passes SQLite
  foreign-key validation before retry.
- Production deployment state for the weekly and fridge-menu backends is not
  confirmed by this document.
- Shopping runtime remains disabled unless a later state update proves otherwise.
- Exact-main GHCR publication is manual, SHA-bound, and digest-authoritative.
  The hosted workflow scans image metadata, every recoverable layer, and the
  final filesystem before publishing `IMAGE_SECRET_FINDINGS=0`. Registry
  publication and production deployment remain separate gates.
- Production-host deploy-capacity qualification is read-only and must complete
  before a separately authorized pull or deployment task.
- Production deployment, Memory OS reconciliation, and Telegram runtime
  troubleshooting now have repository-local procedural skill contracts with
  explicit safety, failure, and verification gates.
- Production migration authority documents are generated from the consumer-owned
  closed field sets, written as collision-resistant root-private canonical JSON,
  and rejected on lifecycle reordering, replay, or bound-input drift.

## 3. Active Blockers

### P0 ? External Qwen benchmarks confirmed no rollout-eligible provider

The following benchmark gate is historical deployment-eligibility evidence. It
does not negate the repository-level, opt-in provider wiring recorded in the
current-state summary above: no Qwen route is active in production, and the
benchmark harness is not evidence of a deployable runtime.

Confirmed state:

- Approved repository/docs-closure main for this state update:
  `f45a3c16b49282775d06003948e449d756aa54f2`.
- Earlier Q1 access/schema plus `qwen3.7-plus` benchmark evidence remains
  anchored to repository main `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`.
- Completed `qwen3.6-plus` benchmark evidence is anchored to repository main
  `f45a3c16b49282775d06003948e449d756aa54f2`.
- External Qwen benchmark execution path:
  `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS`.
- External Qwen benchmark context:
  `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`.
- External Qwen credential mechanism: `QWEN_API_KEY`.
- External Qwen endpoint family: `DASHSCOPE_INTL`.
- Current built-in Hermes runtime context for requested Qwen:
  `QWEN_OAUTH_PORTAL_CONTEXT`.
- Current Hermes Qwen runtime proven: `false`.
- Deployable Qwen integration proven: `false`.
- Repository credential resolver used in these external benchmark tasks:
  `false`.
- Benchmark assets remained the three approved sanitized images with SHA256
  `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Request accounting stayed within approved hard caps:
  Q1 used 6 total provider requests (3 access probes, 3 benchmark requests),
  and the `qwen3.6-plus` benchmark used exactly 3 provider requests with
  0 access probes, 0 retries, 0 fallbacks, 0 repair requests,
  0 Telegram requests, 0 production DB opens, 0 production DB writes, and
  0 Qdrant requests.
- `qwen3.7-plus`, `qwen3.6-plus`, and `qwen3.6-flash` each produced one
  schema-valid access response on `02_simple_plate.jpg` and each passed the
  local validator within the task-scoped DashScope-compatible benchmark
  context.
- Access/schema success proved operational reachability and contract
  compatibility only within that external benchmark context; it did not prove
  current built-in Hermes runtime compatibility and did not prove food quality.
- `qwen3.7-plus` completed the earlier three-image benchmark and remained
  `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE`, `benchmark_candidate=false`.
- `qwen3.6-plus` has now completed the same three-image benchmark and remained
  `QWEN36_PLUS_FAIL_CLOSED_COMPATIBLE`, `benchmark_candidate=false`.
- Historical `qwen3.6-flash` quality evidence has no recoverable exact receipt;
  its detailed diagnostics remain unknown and must not be reconstructed.
- The replacement `qwen3.6-flash` execution on main
  `caadf124d006a543af012ac2b9b42343fc7524d0` used manifest SHA256
  `46eeef07535bf814167e2dab8c8c700ff4de14e1d47ecf7f8cfab21f6f3896c3`
  and exactly 3 requests, 0 retries, and 0 fallbacks. Receipt SHA256 is
  `7b6c07a2912237bf353407ff3806560bce5b1b5ebd54b9f40b362f96f00efdc6`.
- Replacement aggregate precision was `1.0`, recall `0.3333333333333333`,
  sauce recall `0.0`, unsafe aggregate count `0`, and invalid aggregate count
  `2`. Fixture B matched three normalized components; fixtures A and C were
  schema-invalid. The quality gate failed and no Qwen model became eligible.
- `qwen3-vl-8b-instruct` remains `QWEN_FAIL_CLOSED_COMPATIBLE`,
  `benchmark_candidate=false`.
- `qwen3.6-plus` safety and schema handling passed: schema validity `3/3`,
  validator pass `3/3`, aggregate nutrition violations `0`, invalid staging
  `0`, ambiguity handling pass `true`, confirmation correctness `1.0`, and no
  unsafe diary staging observed.
- `qwen3.6-plus` component-grounding quality failed the existing gate:
  mixed-plate precision `0.666667`, mixed-plate recall `0.666667`,
  mixed-plate sauce recall `0.5`; simple-plate precision `0.0`, simple-plate
  recall `0.0`, simple-plate sauce recall `1.0`; aggregate precision
  `0.222222`, aggregate recall `0.555556`, aggregate sauce recall `0.5`.
- The simple-plate `0.0/0.0` result is confirmed evidence. Its cause remains
  open and may reflect model recognition failure, component segmentation
  mismatch, normalized-name mismatch, manifest alias/scoring mismatch, or
  prompt-contract interpretation difference.
- Relative to `qwen3.7-plus`, `qwen3.6-plus` aggregate precision improved,
  aggregate recall improved, sauce recall remained unchanged, confirmation
  correctness remained unchanged, ambiguity remained passing, and both models
  remained below the quality gate.
- Relative to `qwen3-vl-8b-instruct`, `qwen3.6-plus` aggregate precision,
  recall, and sauce recall regressed; schema compatibility remained valid,
  ambiguity handling remained passing, and invalid staging remained zero.
- The three-image benchmark is a bounded release gate and is too small to
  establish general superiority or inferiority of one model over another.
- Eligible providers remain `none`; automatic provider selection remains
  `false`; deployment authorized remains `false`; deployment blocked remains
  `true`.
- Production vision provider remains Gemini by existing deployment state only;
  this benchmark does not endorse Gemini and does not authorize a provider
  switch.
- Raw provider responses were not stored; secret leakage remained false; raw
  error leakage remained false.
- Production runtime remained unchanged during these external Qwen benchmark tasks.

Current verdict:

`V2-R7F-Q2-B PASS ? QWEN3.6-PLUS BENCHMARK COMPLETED THROUGH APPROVED EXTERNAL DASHSCOPE CONTEXT ? MODEL REMAINS FAIL-CLOSED AND INELIGIBLE ? CURRENT HERMES RUNTIME STILL UNPROVEN ? PRODUCTION UNCHANGED`

Next vision step:

- Keep production on the existing Gemini deployment state until a separately
  approved provider path is proven eligible.
- Perform provider-free forensic analysis of sanitized recognized component
  names and expected manifest mappings before changing prompt, manifest, or
  scoring.
- Do not run a new live Telegram photo smoke until a provider earns an offline
  PASS and a fresh activation playbook is approved.
- Do not automatically authorize `qwen3.6-flash` benchmarking, prompt changes,
  manifest changes, scoring-threshold changes, runtime integration, or deployment.

Evidence:

- `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/summary.json`
- `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/eligibility_decision.md`
- `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/historical_comparison.md`
- `/home/hermes/evidence/s71v2-r7f-q2-a-qwen-context-alignment/20260710T011214Z/summary.json`
- `/home/hermes/evidence/s71v2-r7f-q2-b-qwen36plus/20260710T043420Z/summary.json`
- `/home/hermes/evidence/s71v2-r7f-q2-b-qwen36plus/20260710T043420Z/model_comparison.md`

### P1 — Gemini external authorization remains unresolved

Confirmed state:

- API family: `gemini_developer_api`.
- Authentication mechanism: API key header.
- Header used by runtime: `x-goog-api-key`.
- Authoritative credential source: runtime callable provider.
- Endpoint/auth family match: true.
- Runtime key resolution defect from the earlier stale-key path is fixed.
- Limited re-benchmark evidence proved runtime key resolution per request and a
  live provider response, but Gemini still returned `GEMINI_ACCESS_DENIED` at
  `PROVIDER_HTTP` with HTTP class `4xx`.
- Safe reason, domain and canonical status were not present in the stored
  provider error evidence.
- Successful Gemini text calls with the same credential source are still not
  proven by controlled evidence.

Additional ListModels result recorded from operator workflow:

- Query-parameter API key mode and `x-goog-api-key` header mode both previously
  returned HTTP 403 with `text/html`.
- No Gemini model list was obtained.
- Configured model remains `gemini-2.5-flash`.
- Do not change the Gemini model name only as a naming fix.

Current verdict:

`V1-R4 / V2-R7E-C1 BLOCKED — RUNTIME KEY PROPAGATION FIXED LOCALLY BUT EXTERNAL GEMINI AUTHORIZATION REMAINS DENIED — PRODUCTION UNCHANGED`

Evidence:

- `/home/hermes/evidence/s71v1-r4/20260707T135028Z/summary.json`
- `/home/hermes/evidence/s71v2-r7e-c1-limited-rebenchmark/20260709T134049Z/gemini_diagnostic.json`

Next Gemini step:

- Operator-only read-only external authorization audit or separately approved
  credential/project remediation.

Do not perform until separately authorized: additional Gemini diagnostics, reason probes, credential rotation, new-key creation, production config changes, or Telegram photo smoke for Gemini.

### P2 — Existing weekly-menu draft cannot be published

Confirmed state from the last review report: the draft had 20 entries instead of 21, Sunday dinner was missing, review output contained replacement-question-mark encoding defects, and technical enum slot names appeared in user-facing review output. The draft must stay hidden with `publish=false` and `automatic_regeneration=false`. Before publication, validation must require 3 meal slots for each of 7 days, Unicode rendering must be fixed, enum labels must stay internal, and a separate publish approval is required.

### P3 — CI technical debt

Known state: six Telegram parse-mode failures match the existing baseline. They are not a new regression for this state update, but still require a fix or quarantine with owner and deadline.

## 4. Active Work - Sprint 7.1V2-R7F-Q2-B-DOCS

Status:

`STATUS=QWEN36_PLUS_EXTERNAL_BENCHMARK_RECORDED_FAIL_CLOSED_RUNTIME_UNPROVEN_DEPLOYMENT_BLOCKED_PRODUCTION_UNCHANGED`

Current recorded state:

- Approved current main source for this docs-only state update:
  `f45a3c16b49282775d06003948e449d756aa54f2`.
- Context-alignment evidence path:
  `/home/hermes/evidence/s71v2-r7f-q2-a-qwen-context-alignment/20260710T011214Z`.
- Next-generation Qwen Q1 evidence path:
  `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z`.
- `qwen3.6-plus` benchmark evidence path:
  `/home/hermes/evidence/s71v2-r7f-q2-b-qwen36plus/20260710T043420Z`.
- Recorded external benchmark execution path:
  `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS`.
- Recorded external benchmark context:
  `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`.
- Recorded external benchmark credential mechanism:
  `QWEN_API_KEY`.
- Recorded external benchmark endpoint family:
  `DASHSCOPE_INTL`.
- Current Hermes runtime context for requested Qwen remains:
  `QWEN_OAUTH_PORTAL_CONTEXT`.
- Current Hermes runtime proven: false.
- Deployable integration proven: false.
- Benchmark assets and manifest matched the previously approved benchmark set.
- `qwen3.7-plus`, `qwen3.6-plus`, and `qwen3.6-flash` each passed the
  access/schema probe on `02_simple_plate.jpg` within the task-scoped
  DashScope-compatible benchmark context.
- `qwen3.7-plus` and `qwen3.6-plus` have both completed the three-image quality
  benchmark and both remained below the quality gate.
- `qwen3.6-plus` aggregate metrics are precision `0.222222`, recall `0.555556`,
  sauce recall `0.5`, confirmation correctness `1.0`, ambiguity gate `true`,
  aggregate nutrition violations `0`, and invalid staging `0`.
- `qwen3.6-plus` final classification is
  `QWEN36_PLUS_FAIL_CLOSED_COMPATIBLE` with `benchmark_candidate=false`.
- The replacement `qwen3.6-flash` v2 benchmark failed with two schema-invalid
  fixtures; `benchmark_candidate=false` and model eligibility remains `FAIL`.
- Eligible providers: none.
- Automatic provider selection: false.
- Deployment authorized: false.
- Deployment blocked: true.
- Provider requests during this docs task: 0.
- Telegram requests during this docs task: 0.
- Production DB opens/writes during this docs task: 0 / 0.
- Qdrant requests during this docs task: 0.
- Production runtime remained unchanged.

Repository state that remains true:

- Stage-1 vision requires a component-grounded structured inventory schema.
- Model-generated aggregate nutrition is rejected at local validation time.
- Invalid, low-confidence, or ambiguous outputs cannot stage a diary-ready
  pending meal.
- Stage-1 returns a clarification/component summary instead of pending save
  totals when validation succeeds.
- Offline mixed-plate quality fixtures and thresholds are present in the test
  suite.
- Text, weekly, shopping, memory, Qdrant and Telegram routing remain isolated
  from the vision-provider benchmark path.
- One provider request is allowed per one vision turn.
- No Qwen-to-Gemini fallback is allowed for vision.

## 5. Previous V2 Attempt History

- The original V2 attempt was aborted.
- Old approved base:
  `d80526905135dbcf6df2f034fdfcd51463a889a3`.
- Project remote main at abort time:
  `20f1469dc395130fbde30b9736750e247e9b8306`.
- No branch, worktree or patch was created for that obsolete base.
- The blocker was closed by the updated V2-R1 playbook.

## 6. Historical Benchmark Sequence (Production Eligibility Only)

The sequence below remains applicable to any future production activation or
model-quality decision; it is not a prohibition on repository-only routing
contracts and tests:

For Qwen:

1. Perform provider-free forensic analysis of the sanitized recognized
   component names and expected manifest mappings for the completed
   `qwen3.6-plus` benchmark.
2. Determine whether the simple-plate `0.0/0.0` failure reflects genuine visual
   misrecognition, naming/normalization mismatch, component grouping, scoring
   alias limitations, or prompt-contract behavior.
3. Do not change prompt, manifest, aliases, thresholds, runtime integration, or
   deployment policy before that provider-free analysis is complete and reviewed.
4. Treat the replacement `qwen3.6-flash` failure as evidence for provider-free
   schema forensics; do not authorize another model until benchmark parity and
   diagnostic observability are established.
5. Reusing the same benchmark context would establish controlled external
   benchmark-path model quality only; it would not prove current Hermes OAuth
   runtime compatibility, production integration readiness, or deployment
   authorization.
6. No repeat access probe is necessary only if the credential mechanism,
   endpoint family, model alias, and client/request shape remain unchanged.

For Gemini:

- Treat runtime key propagation as locally fixed but external authorization as
  unresolved.
- Wait for operator-approved external auth remediation before any new Gemini
  live request.
- Do not treat the limited re-benchmark 403 alone as permission to change the
  deployed provider automatically.

For rollout decisions:

- Eligible providers remain none.
- Automatic provider selection remains false.
- Production deployment changes remain blocked until a provider becomes
  rollout-eligible under controlled evidence.

## 7. Mandatory Codex Rules

- Always check the exact base SHA gate before source changes.
- Abort when the approved project base no longer matches the project remote.
- Use a separate clean worktree for implementation or docs tasks.
- Do not modify the dirty canonical checkout.
- Do not build, deploy, restart or recreate production without explicit approval.
- Production DB writes default to 0.
- Qdrant changes default to 0.
- Do not write secrets, private IDs or raw provider responses into reports.
- Task-scoped vision provider changes must not affect text or weekly providers.
- Keep docs-only changes separate from implementation diffs unless the current
  task explicitly requires a same-PR state update.
- Update this file in the same PR that changes confirmed project state.

## 8. Unknown Before Production Deploy

Before any production deployment decision, re-confirm: production source SHA, image-digest-to-source mapping, effective feature flags, whether a Qwen path still depends on a task-scoped DashScope-compatible benchmark harness or a separately aligned Hermes runtime path, final authoritative runtime config location, final remediation PR status, Google Console audit result, and whether the current production DB has all weekly/shopping tables.

## 9. Update Rules

- Update this file in the same PR that changes confirmed project state.
- Use patch/minor/major versioning.
- Move superseded state into `docs/CURRENT_STATE_CHANGELOG.md`.
- Never store secrets, credentials, private identifiers or raw provider responses
  in this file.
- Mark unverifiable facts as `UNKNOWN` or `NOT CONFIRMED`.


## 10. Memory Graph V3 (PR-3 through PR-4.1)
- Persistent Derived Graph Storage is implemented and tested.
- Deterministic snapshot hashing bug in GraphSnapshot.create was fixed.
- Atomic publish semantics with cryptographic read-back verification are active.
- The capability operates offline and does not mutate authoritative facts.


Persistent derived graph storage implemented offline.
SQLite Memory OS remains authoritative.
Graph store remains DERIVED_REBUILDABLE.
No runtime activation.
No production graph migration.

PR-3:
COMPLETE

PR-3.1:
COMPLETE

PR-3.2:
COMPLETE

PR-3.3:
COMPLETE

PERSISTENT_DERIVED_GRAPH_STORAGE=COMPLETE_OFFLINE

PR-4=
FOUNDATION_MERGED

PR-4_FOUNDATION_PR=199
PR-4_FOUNDATION_HEAD=6a322c8ad2170b4e5a659fc6de2c7966ef4878d8
PR-4_FOUNDATION_MERGE=2ab4daed932e0f7b4b088afcfc4e79f635fa830e
PR-4_FOUNDATION_CI_SEQUENCE=PASS

POST_MERGE_FINDINGS=
- lexical/numeric authoritative source-state ordering produced false stale reads;
- placeholder tests overstated adversarial evidence;
- the claimed pure Layer A remained mixed into the coordinator.

PR-4.1=INTEGRITY_CLOSURE_IMPLEMENTED_CANDIDATE
FULL_SOURCE_FRESHNESS_COMPLEXITY=O(n)
## 11. Memory Graph V3 (PR-5)

PR-5.0 (#201):
FOUNDATION_MERGED
EXACT_HEAD_TESTS=FAIL

PR-5.1 (#202):
PARTIAL_CLOSURE_MERGED
EXACT_HEAD_TESTS=FAIL
ADMIN_MERGE_USED=true

PR-5.2 (#203):
PR5_COMPLETE=true
PR6_TECHNICAL_COMPLETE=true
PR7_ALLOWED=true

GRAPH_CONVERGENCE_RUNTIME_ACTIVATED=false
PRODUCTION_CHANGED=false
