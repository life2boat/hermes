# CURRENT_STATE changelog

## 1.2.65 - 2026-08-18 (PR-4.1 integrity closure candidate)
Corrected:
- PR #199 provided the deterministic graph read-path foundation at head
  `6a322c8ad2170b4e5a659fc6de2c7966ef4878d8`, squash-merged as
  `2ab4daed932e0f7b4b088afcfc4e79f635fa830e`; its pre-merge CI sequencing
  passed.
- an independent exact-main audit found that lexical canonical fact-ID order
  was compared positionally with numeric SQLite row order, producing false
  `STALE_GRAPH` results at the 9/10 boundary;
- the audit also found placeholder adversarial tests and that the documented
  pure Layer A remained mixed into the database coordinator.

Changed:
- PR-4.1 implements canonical complete source-state equality, a genuinely pure
  structural query layer, fail-closed structural validation and authoritative
  hydration, and real adversarial/read-only evidence.
- full-source freshness comparison is truthfully `O(n)` in authoritative fact
  count and is bounded by existing Memory graph fact limits.
- PR-4.1 remains `INTEGRITY_CLOSURE_IMPLEMENTED_CANDIDATE`; PR-5 remains
  `BLOCKED_PENDING_PR4_1_EXACT_MAIN`.

## 1.2.64 - 2026-08-18 (PR-4 Complete)
Changed:
- PR-4 Deterministic Graph Read Path implemented and verified against all exact-schema/adversarial test matrices.
- Graph query layer established in `gateway/memory/graph_query.py`.
- No runtime activation; `GRAPH_QUERY_RUNTIME_ACTIVATED` remains `false`.

## 1.2.63 - 2026-08-18 (PR-4 Candidate)
Changed:
- PR-3.3 confirmed as COMPLETE.
- PR-4 Deterministic Graph Read Path implemented as candidate.

## 1.2.62 - 2026-08-18 (PR-3.3 Candidate)
Changed:
- PR #197: head 7a644ec8771ffef83bddd8b9a84d900833585902, merge 185030b05033ee3779ce8f0e74f9572aa24a0640, CI sequencing PASS.
- PR-3.3: table_xinfo exact-column closure candidate.

## 1.2.61 - 2026-08-18 (PR-3.2 Candidate)
Changed:
- Exact schema verification and partial schema semantics implemented.
- Removed ad-hoc print statements from classifier.
- PR #195 (PR-3): persistent store foundation.
- PR #196 (PR-3.1): integrity/transaction closure, final head 3b32c5952a06a8b21aae5c3be4a5258c68c2a1ae, merge 47ddc93d8727b67f272e7e6d70b25d92711fc350, CI sequencing PASS.
- PR-3.2: exact-schema/SOT closure candidate.

## 1.2.60 - 2026-08-18 (PR-3.1 Candidate)
Changed:
- Hardened persistence layer with exact schema classification (\classify_memory_graph_store_schema\), rigorous transaction boundary handling (\SAVEPOINT\ cleanup), test-only failure injection hooks, exact JSON byte serialization, count validations, and \PRAGMA foreign_keys = ON\ enforcement.
- Updated CURRENT_STATE.md to reflect PR-3 FOUNDATION_MERGED, PR-3.1 IMPLEMENTED_CANDIDATE, and PR-4 PENDING_PR3_1_CLOSURE.
- Rationale: Resolve PR-3 integrity defects before moving to PR-4 read path.


## 1.2.59 - 2026-08-18

Changed:
- Updated CURRENT_STATE.md to mark PR-2 foundation COMPLETE.
- Recorded implementation of PR-2.2 Canonical Source-State & Evidence Closure.
- Documented canonical authoritative-source ordering and source-state JSON identity binding.
- Truthfully recorded PR #193 historical record: PR #193 was merged via `--admin` before Tests/Nix CI terminal state. Eventual exact-head CI did PASS, but reported test cases were inaccurate and source ordering remained incomplete until PR-2.2.
- Updated ADR-0082 to reflect python string bounds, exact boundary privacy semantics, projection result immutability, and 499 worst-case derivations.

Safety & Provenance:
- the projection is purely structural, provider-free, and contains no LLM invocation.
- full adversarial test suite covers bounds, privacy, and identity generation.
- no production mutation or database schema changes occurred.

## 1.2.58 - 2026-08-18

Changed:
- recorded PR-2 completion: Authoritative SQLite -> Deterministic Graph Projection Engine.
- established `gateway/memory/graph_projection.py` generating bounded deterministic snapshots.
- implemented `PROJECTION_LIMIT_EXCEEDED` and `CROSS_USER_INPUT_REJECTION` bounds checking.
- created ADR 0082 for graph projection engineering design.

Safety & Provenance:
- the projection is purely structural, provider-free, and contains no LLM invocation.
- full adversarial test suite covers bounds, privacy, and identity generation.
- no production mutation or database schema changes occurred.

## 1.2.57 - 2026-08-18

Changed:
- recorded PR #190 early-merge sequencing truth: merged before all CI terminal.
- implemented duplicate JSON key rejection fail-closed in graph contract deserialization.

Safety & Provenance:
- PR #190 (PR-1.3 closure) was merged at `2026-08-17T23:12:05Z` (merge commit `1afb2a788ae8bb071c47d5c70829f29e7e53106a`, head `25a0b6e29ecde3832272344ddc645cdb16c8cf7e`).
- PR #190 Nix workflow reached terminal state at `2026-08-17T23:15:23Z` (PASS).
- `PR190_MERGED_BEFORE_ALL_CI_TERMINAL=true`
- `PR190_MERGED_ONLY_AFTER_ALL_TECHNICAL_CI_TERMINAL=false`
- no rollback required from this sequencing defect.


Changed:
- closed the post-merge source-of-truth state for PR #189 (forward recovery of PR #188) and PR-1.3 (contract parity closure).
- verified exact-main test suites, JSON Schema Draft202012 parity tests, and independent module guards all pass.

Safety & Provenance:
- PR #188 accidentally truncated graph module/test files; PR #189 restored them from pre-corruption blobs and added zero-test guards.
- PR-1.3 implemented strict schema parity closure and robust production module guards without any production mutations.
- no production, database, Qdrant, secrets, deployment, or live runtime changed.

## 1.2.55 - 2026-08-17

Changed:
- closed ProductionRuntimeAttestation v1 source of truth across B1 (PR #181) and B2 (PR #182).
- documented B2 read-only collectors (Docker, SQLite, Qdrant, Secret Source Structural).
- recorded PR #183 post-health lookup correction (repository fix, not a production repair).
- recorded authoritative exact-main ProductionRuntimeAttestation with comparison MATCH.

Safety & Provenance:
- PR #182 merged via squash; PR head is not ancestor due to squash merge. Correct proof established: `PR_HEAD_TREE_SHA == MERGED_TREE_SHA`.
- historical post-collection health proof remains `INSUFFICIENT_EVIDENCE` (PR #183 corrects future runs).
- no production mutation, new live collection, deployment, or DB write occurred.
- `production_sha` remains unknown and execution provenance remains UNPROVEN.

## 1.2.54 - 2026-08-17

Changed:
- added the clean-forward HealBite Secret Remediation R1 source closure from canonical base `e85ca7dbee2025320c5daf61181a6c1142f18a9b`;
- retained `f438671ee445ae5a73a2aad235298fe5f1439536` only as forensic reference while keeping `HISTORICAL_EXECUTION_PROVENANCE=UNPROVEN`;
- added strict JSON byte-span removal, child-only Compose interpolation, verified-process protected-name capture, bounded poller convergence, container-scoped health, and exact source/runtime authority contracts.

Safety:
- `PRODUCTION_FORENSIC_RUNTIME_STATE=PASS` and `PRODUCTION_REPAIR_REQUIRED=false`;
- repository source closure only; no production, database, Qdrant, secrets, containers, deployment, rollback, or live runtime changed.

## 1.2.52 - 2026-08-14

Changed:
- closed the post-merge source-of-truth state after PR #176 implemented authoritative
  semantic verification for Effective Policy (PR-5.1) at canonical main
  `dbebea42967ed0bb2d4f5f95da01fca32c5d0723`, successfully closing H-V1-PR5-001.
- verified that `verify_effective_policy_report()` provides fail-closed authoritative
  defense against forged invariant and required gate resolutions, with full 100% CI pass.

Safety:
- the closure is repository documentation and offline contract verification only;
  no production, database, Qdrant, secrets, deployment, or live runtime changed.

## 1.2.51 - 2026-08-14

Changed:
- closed the post-merge source-of-truth state after PR #175 finalized canonical
  adoption and lifecycle integration of Hermes Intent Control Plane v1 at
  canonical main `112b865f7a2906bdf33e302d7a7a1d2118db1826`.

Safety:
- the closure is repository documentation and offline contract integration only;
  no production, database, Qdrant, secrets, deployment, or live runtime changed.

## 1.2.50 - 2026-08-14

Changed:
- closed the post-merge source-of-truth state after PR #174 merged Effective Policy
  and Source Attribution (PR-5) at canonical main
  `432aacfd0fd30e65fd788bec8d8f8ec934a8e1ef`.

Safety:
- the closure is repository documentation only; no production, database,
  Qdrant, secrets, deployment, or runtime changed.

## 1.2.49 - 2026-08-14

Changed:
- closed the post-merge source-of-truth state after PR #171 (PR-4 Evidence-Bound
  Convergence) and PR #172 (PR-4.1 H-PR4-001 public boundary integrity) merged
  at canonical main `a99663f6e6bdf5bde7c738ff844c1fa7f09c0f32`.

Safety:
- the closure is repository documentation only; no production, database,
  Qdrant, secrets, deployment, or runtime changed.

## 1.2.48 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #163 merged the
  canonical staged Memory Convergence migration at canonical main
  `b3653ca0ddc9841291cdfee80abe47ba126067de`.

Safety:
- the closure is repository documentation only; no production, database,
  Qdrant, secret, feature-flag, deployment, runtime, or live-smoke state changed.

## 1.2.47 - 2026-08-13

Changed:
- added the ordered canonical `memory_convergence` staged migration component,
  strict additive schema classification, transactional legacy intent seed, and
  read-only production runtime schema validation through one shared schema
  authority.

Safety:
- validation uses disposable SQLite only; production, Qdrant, providers,
  secrets, feature flags, deployment and live runtime were not accessed or
  changed.

## 1.2.46 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #161 merged Memory
  Convergence v1.1 at canonical main
  `cbb37920af6e204f3a86d6dcabb37250b7269bd9`.

Safety:
- the closure is repository documentation only; no production, database,
  Qdrant, secret, feature-flag, deployment, runtime, or live-smoke state changed.

## 1.2.45 - 2026-08-13

Changed:
- accepted Memory Convergence v1.1 with gateway-owned bounded startup/periodic
  reconciliation, privacy-safe alertable health, owner-scoped repair and
  read-only historical orphan classification.

Safety:
- tests use disposable SQLite and fake clients only; production migration,
  vector activation, live Qdrant scan/delete, provider calls and deployment
  were not performed.

## 1.2.44 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #159 merged durable
  SQLite-to-Qdrant memory convergence at canonical main
  `ac9b4f0e4d8d7a1d117f1fa4301bf2d138e95ca0`.

Safety:
- the closure is repository documentation only; no production, database,
  Qdrant, secret, feature-flag, deployment, runtime, or live-smoke state changed.

## 1.2.43 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #157 merged the thin
  Antigravity executor context bridge at canonical main
  `e102b64dcfdea0120c891ba1298fecfd6c75cb62`.

Safety:
- no production, provider, database, Qdrant, secret, feature-flag,
  deployment, runtime, or live-smoke state changed.

## 1.2.42 - 2026-08-13

Added:
- added a thin Antigravity workspace rule, task-bootstrap skill, and read-only
  onboarding prompt that reference the canonical Hermes engineering contracts;
- recorded serialized executor ownership: Antigravity primary, Codex reserve
  after explicit handoff, and Manus read-only research and assurance.

Changed:
- advanced current-state verification to canonical main
  `cbeb9535ae9cac0c1bca382d7f86fa7172f74722` before the bounded integration PR.

Safety:
- the adapters grant no production, provider, database, Qdrant, secret,
  feature-flag, deployment, runtime, or live-smoke authority.

## 1.2.41 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #155 bound the exact
  Food Vision Quality V3 human-review PASS at canonical main
  `6e433bc9a30ed0211d26123cc852be73dc88ed58`.

Safety:
- V3 remains `CANDIDATE`, no provider model was selected or executed, and no
  production, database, Qdrant, secret, feature flag, deployment, or runtime
  state changed.

## 1.2.40 - 2026-08-13

Added:
- bound the operator's visual PASS to the exact V3 manifest, review package,
  and three fixture hashes through a closed, sanitized, immutable review
  receipt using role-only reviewer provenance;
- required V3 dry runs and provider executions to validate that receipt before
  entering the provider or credential boundary.

Changed:
- recorded benchmark reference truth as human-reviewed while retaining the
  documented `CANDIDATE` lifecycle and leaving model eligibility `FAIL`.

Safety:
- no fixture, manifest semantic, threshold, provider receipt schema, model,
  provider request, production, database, Qdrant, secret, feature, deployment,
  or runtime state changed.

## 1.2.39 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #153 merged the
  product-aligned `food_vision_quality_v3` candidate contract at canonical main
  `57b4376464d0d40926320ced73e5d4b601dea86e`.

Safety:
- lifecycle remains `CANDIDATE`, human visual review remains `NOT_PERFORMED`,
  and no provider model was selected or executed;
- v1/v2 fixture bytes and contracts remain unchanged, and production, database,
  Qdrant, secrets, feature flags, deployment, and runtime remain unchanged.

## 1.2.38 - 2026-08-13

Added:
- added provider-neutral `food_vision_quality_v3` as a `CANDIDATE`
  successor with a closed ambiguity contract, unsupported-specificity outcome,
  digest-bound review package, and provider-free dry-run support;
- reused the exact three v2 image hashes without copying or modifying fixture
  bytes; retained A/B controls and replaced only C reference semantics with the
  runtime-canonical generic `sauce` plus clarification policy.

Changed:
- extended the harness allowlist and scorer additively for v3 while retaining
  runtime schema `food_vision_inventory_v1`, receipt schema version 3, three-
  request budget, zero retries/fallbacks, and unchanged quality thresholds;
- recorded Fixture D as not required because existing fixtures already cover
  exact recognition, distractor rejection, resolvable condiments, and visual
  ambiguity.

Safety:
- v1/v2 manifests, expectations, provenance and image bytes remain unchanged;
- human visual review remains `NOT_PERFORMED`, no provider model is selected or
  executed, and production/database/Qdrant/secret/feature/deployment state is
  unchanged.
## 1.2.37 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #151 merged the bounded
  food-Vision receipt-v3 schema observability repair at canonical main
  e97df4b4804aa637a6992c7b64f6d94836d3d3db.

Safety:
- no provider request, production, database, Qdrant, secret, feature flag,
  deployment, or runtime activation occurred;
- historical v2 manifest, images and receipt, quality thresholds, Prompt
  corpus digest, lifecycle, and human-review state remain unchanged.

## 1.2.36 - 2026-08-13

Added:
- bound the exact sanitized replacement receipt by SHA256 and completed
  provider-free schema, runtime-parity, and fixture-validity forensics;
- added receipt schema version 3 with closed local validator reason codes and
  coarse trigger summaries for future schema-invalid evidence.

Changed:
- recorded benchmark/runtime schema parity as passing for
  food_vision_inventory_v1;
- recorded historical A/C trigger detail as unrecoverable from immutable
  receipt v2, Fixture A as low-complexity schema nonconformance, and Fixture C
  as product-relevant but visually ambiguous at the exact sour-cream label;
- classified v2 as requiring a new immutable successor rather than changing
  historical manifest or fixture bytes.

Safety:
- no provider request was performed and no provider response, prompt, image,
  identifier, path, credential, or request payload is added to receipt v3;
- prompt corpus digest/lifecycle and all production, database, Qdrant, secret,
  feature flag, deployment, and runtime state remain unchanged.

## 1.2.35 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #149 merged the current-main
  Qwen `qwen3.6-flash` replacement evidence at canonical main
  `e4305e773db60e28b5a2a10071c3ecac48dceac9`;
- recorded PR #146 as superseded by the semantic current-main replacement rather
  than merged from its stale conflicting branch.

Safety:
- prompt corpus digest and lifecycle remain unchanged;
- no provider request, production, database, Qdrant, secret, feature flag,
  deployment, or runtime activation occurred.
## 1.2.34 - 2026-08-13

Added:
- recorded durable replacement `food_vision_quality_v2` evidence for
  `qwen3.6-flash` on canonical main
  `caadf124d006a543af012ac2b9b42343fc7524d0`;
- recorded receipt SHA256
  `7b6c07a2912237bf353407ff3806560bce5b1b5ebd54b9f40b362f96f00efdc6`,
  exact request policy (3 requests, 0 retries, 0 fallbacks), sanitized outcomes,
  and aggregate metrics.

Changed:
- distinguished the lost historical v2 receipt and unresolved diagnostics from
  the new replacement execution;
- recorded that the replacement quality gate failed and no Qwen model is
  rollout-approved;
- preserved Prompt System v1, prompt corpus digest
  `d52adea60862ad5ca2b71a23dfd506adc02ca8dcb3b6270ab79a51bc949c86ea`,
  lifecycle `CANDIDATE`, and human review `NOT_PERFORMED`.

Safety:
- no provider request, production, database, Qdrant, secret, feature flag,
  deployment, or runtime activation occurred; the private receipt remains
  outside the repository.

## 1.2.33 - 2026-08-13

Changed:
- closed the post-merge source-of-truth state after PR #147 merged the Prompt
  Engineering and Prompt Quality System at canonical main
  `9f5e8ff03d4bfbb673292775082a8801002a3e32`;
- preserved the prompt corpus lifecycle state `CANDIDATE` and human review state
  `NOT_PERFORMED` while recording its provider-free technical gate as PASS.

Safety:
- no provider call, runtime, production, database, Qdrant, secret, credential,
  deployment, or live-smoke action occurred;
- raw prompts, dynamic payloads, private reasoning, and raw chain-of-thought remain
  forbidden trace evidence.

## 1.2.32 - 2026-08-12

Added:
- added PromptSpec schema/compiler/validator/linter contracts, versioned prompt
  provenance, Behaviour Trace schema v2 prompt evidence, and a sanitized eight-case
  provider-free prompt-quality corpus;
- added exact-head prompt-quality CI, canonical prompt authoring/eval/failure docs,
  ADR-0078, prepared-context inclusion, and the mandatory AGENTS prompt rule.

Changed:
- preserved Behaviour Trace schema-v1 replay while making schema v2 the current
  writer contract;
- recorded the prompt corpus as technical PASS but lifecycle CANDIDATE with human
  review explicitly NOT_PERFORMED.

Safety:
- no provider call, runtime, production, database, Qdrant, secret, credential,
  deployment, or live-smoke action occurred; raw prompts and private reasoning remain
  forbidden trace evidence.

## 1.2.31 - 2026-08-12

Changed:
- closed the post-merge source-of-truth state after PR #144 merged PR-6 at
  canonical main `0498663186123d0b0568d2cc56ac498d59939a34`;
- marked the Hermes AI Engineering System v2 repository-contract foundation
  complete while preserving review-only Failure-to-Eval and procedure-maturity
  boundaries.

Safety:
- the GOLDEN corpus remains unchanged at digest
  `e2580fb10c6d02a55ace0efc9092bd6f3092a9a3a188515c5dba32b44708c8c7`;
- no runtime, production, database, Qdrant, secret, provider, deployment, or
  live-smoke action occurred.

## 1.2.30 - 2026-08-12

Added:
- added the PR-6 implementation candidate: deterministic Failure-to-Eval
  candidate construction, candidate-only output storage, and procedure-maturity
  receipts with explicit agent-judgement and authority-separation evidence.

Changed:
- updated the v2 lifecycle and prepared context to reference executable policy.

Safety:
- the Golden corpus and its digest remain unchanged; no runtime, production,
  database, Qdrant, secret, provider, deployment, or live-smoke action occurred.

## 1.2.29 - 2026-08-11

Added:
- added release gate schema/policy version `1`, closed merge/production targets,
  sensitivity-derived requirements, source identity, technical blockers,
  governance observations, canonical receipts, and PASS/FAIL/BLOCKED CLI exits;
- added the read-only exact-head `Agent Release Gate` workflow with independent
  code, GOLDEN offline behaviour, secret-scan, and adversarial evidence.

Changed:
- marked executable merge and production-release aggregation implemented while
  keeping merge eligibility distinct from production authority;
- verified repository documentation against canonical main
  `79d8c5bb7f75f479a4277ab255c633fae685cb80`.

Safety:
- the approved GOLDEN corpus and digest are unchanged;
- no product runtime, production, containers, SQLite, Qdrant, secrets,
  provider calls, live smoke, build activation, or deployment were changed.


## 1.2.28 - 2026-08-11

Added:
- added executable model policy version `1`, the closed task/model/reasoning
  matrix, explicit substitution/provider-change validation, and sanitized
  deterministic model-policy receipts;
- added cost policy version `1`, complete call-category accounting, typed
  budgets, deterministic decimal estimation, external rate-card schema version
  `1`, canonical pricing identity, currency checks, and LLM Ops receipts;
- added the offline `scripts/check_llm_ops_policy.py` CLI and focused synthetic
  tests without provider calls or real pricing claims.

Changed:
- marked model selection and cost-budget evaluation implemented while keeping
  release-gate aggregation and CI enforcement explicitly not implemented;
- verified repository documentation against canonical main
  `72087833d868fbbd7015b7e50e9d17891bf99e69`.

Safety:
- the approved GOLDEN corpus and digest are unchanged;
- no product runtime, production, containers, SQLite, Qdrant, secrets,
  provider calls, or network-based evals were changed.


## 1.2.27 - 2026-08-11

Added:
- promoted the versioned 49-case deterministic behaviour corpus to GOLDEN
  after explicit human/operator review;
- recorded the approval anchor: candidate head
  `fa77b12cb9a0f1b1e8b0eaa596cd41092fdfdb20`, dataset version
  `agent-behaviour-v1`, engine version `1`, and immutable corpus digest
  `e2580fb10c6d02a55ace0efc9092bd6f3092a9a3a188515c5dba32b44708c8c7`.

Changed:
- corpus lifecycle state is GOLDEN; promotion metadata is intentionally
  excluded from the immutable behavioural-content digest.

Safety:
- approval remains valid only while the exact corpus digest is unchanged;
- no product runtime, production, containers, SQLite, Qdrant, secrets,
  provider calls, or network-based evals were changed.


## 1.2.26 - 2026-08-11

Added:
- added deterministic behaviour graders, a closed assertion registry, the
  offline eval runner/CLI, stable reports, and baseline comparison;
- added a 49-case synthetic/sanitized corpus across nine categories with 43
  explicit critical cases and a corpus-digest-bound review table.

Changed:
- advanced the scenario contract to schema v2 with required
  canonical_source_or_fixture_version while preserving identifiable v1 reads
  and v1 canonical serialization;
- updated Source Map, System Model, behaviour contracts, Current State, and the
  Knowledge Pack to separate technical implementation from golden promotion.

Safety:
- corpus status remains CANDIDATE; human review is NOT_PERFORMED and must bind
  exact dataset version, corpus digest, and PR head before merge;
- model/cost policy, release aggregation, CI gating, and failure-candidate
  automation remain not implemented;
- no product runtime, production, containers, SQLite, Qdrant, secrets, provider
  calls, or network-based evals were changed.


## 1.2.25 - 2026-08-11

Added:
- added the stdlib-only ai_engineering package with closed schema-version 1
  behaviour trace and scenario contracts;
- added recursive sanitized-evidence validation, canonical JSON, SHA-256
  identity, bounded fixture loading, provider-free replay, and synthetic trace
  fixtures.

Changed:
- updated the v2 contracts, Source Map, Current State, and Knowledge Pack entry
  to distinguish the implemented trace/replay substrate from planned graders
  and release automation;
- updated repository-state verification to canonical main
  `6c2aa61755eb213c4d64bfd7c269e526723f9e86`.

Safety:
- product runtime, production, containers, SQLite, Qdrant, and secrets were not
## 1.2.24 - 2026-08-11

Added:
- established the Hermes AI Engineering System v2 behaviour, evaluation,
  LLM Ops, release-gate, and procedure-lifecycle contracts;
- added ADR-0074 through ADR-0077 and Knowledge Pack navigation.

Changed:
- extended the v1 lifecycle, Source Map, System Model, and Invariants with the
  v2 contract layer;
- updated repository-state verification to canonical main
  `160262d5f87254a26e8791b7637ec960c386b791`.

Safety:
- executable behaviour evaluation, release-gate aggregation, cost evaluation,
  and CI behaviour gates remain `NOT_IMPLEMENTED`;
- production, runtime, containers, SQLite, Qdrant, and secrets were not changed.

## 1.2.23 - 2026-08-09

Added:
- recorded the normal merge of PR #129 and the complete operational-adoption
  documentation layer;
- recorded the opt-in, provider-registry Qwen/DashScope Vision route and the
  explicit separation from Qwen Portal OAuth;
- recorded durable multimodal sanitization plus ownership-bound, turn-scoped
  Telegram image cleanup.

Changed:
- updated repository-state verification to the PR #129 merge commit
  `713c90a1849d5bc415f6ab8378345b0f67415df1`;
- made ambiguous bare `qwen` auxiliary routing fail closed while preserving
  explicit `alibaba`, `qwen-dashscope`, and `qwen-oauth` identities.

Safety:
- no default Qwen model, cross-provider image fallback, or production feature
  activation was introduced;
- production, containers, SQLite, Qdrant, and secrets were not changed.

## 1.2.22 - 2026-08-09

Added:
- recorded the normal merge of PR #128 and its Phase 2 execution layer:
  repository-bound task-context preparation, AI review, and production-
  readiness checklists.

Changed:
- updated repository-state verification to the PR #128 merge commit
  `14064c7291d53f4ea1f7e00e901fbb9dbab08907`.

Safety:
- this state update records repository documentation and automation only;
- production, containers, SQLite, Qdrant and secrets were not changed.

## 1.2.21 - 2026-08-09

Added:
- recorded the normal merge of PR #127 and verified the five Phase 1 ADRs plus
  the six-part Knowledge Pack on canonical main;
- added AI change-review and production-readiness checklists;
- added a repository-bound `prepare_task.py` context collector and focused
  tests for Git binding, tracked-document packaging, safe output boundaries and
  truthful pytest-cache classification.

Changed:
- updated repository-state verification to PR #127 merge commit
  `8cd61d3192bb9b9e0ae507dc5820f6534a7dc52b`;
- made pre-task context preparation mandatory in `TASK_TEMPLATE.md` and linked
  the automation/checklists from the source map and AI Knowledge Pack index.

Safety:
- prepared task context contains repository paths and tracked documentation,
  never secret values or raw pytest failure identifiers;
- pytest cache is explicitly non-authoritative and cannot establish PASS;
- production, containers, SQLite, Qdrant and secrets were not changed.

## 1.2.20 - 2026-08-09

Added:
- recorded a successful Phase 0.5 cold-start usability check based only on the
  five AI-engineering foundation documents;
- added five populated ADRs for semantic memory, SQLite-first dual writes,
  exact-SHA deployment, user/household isolation and the AI engineering system;
- added the six-part `knowledge/` structure and a sanitized scanner-failure
  lesson covering rootfs symlink validation.

Changed:
- updated repository-state verification to the PR #126 merge commit
  `c6d0852b95a068a3bab7528e656da91ab4274a08`;
- linked the source map and system model to the structured Knowledge Pack while
  preserving executable contracts, skills and tests as authority.

Safety:
- changes are repository documentation only;
- runtime/source-code behavior was not changed;
- production, containers, SQLite, Qdrant and secrets were not changed.

## 1.2.19 - 2026-08-09

Added:
- introduced the Phase 0 Hermes engineering foundation: source map, system
  model, cross-system invariant index, AI-agent rulebook, and task template;
- classified Knowledge Pack as the versioned engineering-document layer, not a
  separately implemented runtime component.

Changed:
- updated repository-state verification to canonical main
  `8d87aaabfb613c2c1844a0c9352a0c6c11fedf2b`;
- linked architectural and operational claims to current code, tests, policies,
  skills, ADRs, runbooks and Git evidence.

Safety:
- runtime/source-code behavior was not changed;
- production, containers, SQLite, Qdrant and secrets were not changed.

## 1.2.18 - 2026-08-09

Added:
- recorded the repository-only image-secret remediation candidate: five source
  self-detection rewrites, sixteen pre-layer dependency cleanups, and twenty-nine
  exact evidence-bound exceptions;
- recorded fail-closed exception binding to exact path, rule, package version,
  artifact identity, file hash, and marker shape.

Changed:
- updated repository-state verification to canonical planning base
  248d0f7683889bd4b169996f8603031f36afbfb1;
- marked the expected zero-finding OCI result as NOT CONFIRMED until a post-merge
  exact-main build and full scan.

Safety:
- production, containers, SQLite, Qdrant, and secrets were not changed;
- no production or local production-host image build was performed.

## 1.2.16 - 2026-08-08

Added:
- recorded the canonical staged-migration authority-package producer and its explicit initial-authority, plan, companion, final-authority, validation, and runtime-attestation ordering;
- recorded that operator authorization plus P5B/P6A evidence remain external inputs and are not manufactured by the producer.

Changed:
- updated repository-state verification to canonical planning base `8f9e1a60f5535cf3b1b843f4c1203e9d1a51f20d`;
- recorded plan schema v7 and operation-bound approval/policy schema v2.

Safety:
- production execution, backup, migration, build, deploy, container changes, SQLite writes, Qdrant mutation, and secret changes were not performed.
## 1.2.11 - 2026-07-10

Added:
- recorded the completed `qwen3.6-plus` three-image benchmark evidence path `/home/hermes/evidence/s71v2-r7f-q2-b-qwen36plus/20260710T043420Z`;
- recorded `qwen3.6-plus` external benchmark execution path `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS` and benchmark context `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`;
- recorded `qwen3.6-plus` aggregate metrics as major precision `0.222222`, major recall `0.555556`, sauce recall `0.5`, confirmation correctness `1.000`, ambiguity gate pass `true`, aggregate nutrition violations `0`, and invalid staging `0`.

Changed:
- updated the CURRENT_STATE verification base to project main `f45a3c16b49282775d06003948e449d756aa54f2` for repository-state/docs closure only;
- clarified that `qwen3.6-plus` benchmark validity is real for the external DashScope task-scoped harness, but current Hermes OAuth runtime compatibility and deployable Qwen integration remain unproven;
- changed `qwen3.6-plus` status from `ACCESS_SCHEMA_PASS / not_benchmarked` to `QWEN36_PLUS_FAIL_CLOSED_COMPATIBLE / benchmark_candidate=false`;
- clarified that `qwen3.6-flash` remains `ACCESS_SCHEMA_PASS` only and not benchmarked;
- replaced the previous recommendation to benchmark `qwen3.6-plus` with a provider-free forensic analysis recommendation for the observed simple-plate `0.0/0.0` result.

Safety:
- provider requests during the recorded benchmark evidence: 3 total, with 0 access probes, 0 retries, 0 fallbacks and 0 repair requests;
- provider requests during this docs task: 0;
- runtime/test/config changes during this docs task: 0;
- production build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.10 - 2026-07-10

Added:
- recorded the Qwen benchmark-context correction evidence path `/home/hermes/evidence/s71v2-r7f-q2-a-qwen-context-alignment/20260710T011214Z`;
- recorded Q1 execution path as `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS`;
- recorded Q1 benchmark context as `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`, with credential mechanism `QWEN_API_KEY` and endpoint family `DASHSCOPE_INTL`;
- recorded the current built-in Hermes Qwen runtime context as `QWEN_OAUTH_PORTAL_CONTEXT`.

Changed:
- updated the CURRENT_STATE verification base to project main `1e048a7479253283ba2087e4e2ef6ad9ca584556` for repository-state/docs closure only;
- clarified that the historical Q1 access/schema and `qwen3.7-plus` benchmark results remain valid, but were produced through a task-scoped DashScope-compatible benchmark harness rather than the current built-in Hermes Qwen OAuth runtime;
- clarified that `qwen3.6-plus` and `qwen3.6-flash` remain `ACCESS_SCHEMA_PASS` / `not_benchmarked` in the same external benchmark context only;
- clarified that current Hermes Qwen runtime compatibility is not proven, deployable Qwen integration is not proven, eligible providers remain `none`, and deployment remains blocked.

Safety:
- provider requests during this docs task: 0;
- runtime/test/config changes during this docs task: 0;
- production build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.9 - 2026-07-10

Added:
- recorded next-generation Qwen access/schema success for `qwen3.7-plus`,
  `qwen3.6-plus`, and `qwen3.6-flash` against approved main
  `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`;
- recorded that only `qwen3.7-plus` received the full three-image benchmark
  under the pre-approved fixed access-priority ordering;
- recorded next-generation Qwen benchmark evidence path
  `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z`.

Changed:
- updated the CURRENT_STATE verification base to project main
  `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`;
- replaced the prior limited `qwen3-vl-8b-instruct` summary with the newer
  next-generation Qwen access audit semantics;
- clarified that all three tested next-generation aliases were operationally
  reachable and schema-valid on the access asset, but only `qwen3.7-plus` was
  quality-benchmarked;
- recorded `qwen3.7-plus` benchmark metrics as major precision `0.111111`,
  major recall `0.444444`, sauce recall `0.5`, confirmation correctness
  `1.000`, ambiguity gate pass `true`, aggregate nutrition violations `0`, and
  invalid staging `0`;
- recorded final classification `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE`,
  `benchmark_candidate=false`, eligible providers `none`, deployment blocked,
  and production remaining on the existing Gemini deployment state only.

Safety:
- provider requests during the recorded evidence: 6 total (3 access probes, 3
  benchmark requests), with 0 retries, 0 fallbacks and 0 repair requests;
- provider requests during this docs task: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.8 - 2026-07-09

Added:
- recorded the limited exact-main vision re-benchmark against approved main `14981980403da56db94c90483bcab4ee209e9784`;
- recorded the single-request Gemini operational result `GEMINI_ACCESS_DENIED` at `PROVIDER_HTTP` with HTTP class `4xx`;
- recorded Qwen limited re-benchmark quality metrics and fail-closed ineligibility from the same evidence set.

Changed:
- updated the CURRENT_STATE verification base to project main `14981980403da56db94c90483bcab4ee209e9784`;
- replaced the older six-request benchmark blocker summary with the newer four-request limited re-benchmark result;
- clarified that no provider is eligible for rollout, automatic provider selection remains false, and production stays on the existing Gemini deployment state only.
- clarified that the three-image limited re-benchmark is bounded evidence for rollout eligibility, not a general quality verdict.

Safety:
- provider requests during the limited re-benchmark evidence: 4 total (1 Gemini, 3 Qwen), with 0 retries, 0 fallbacks and 0 repair requests;
- provider requests during this docs task: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.7 - 2026-07-09

Added:
- shorter provider-neutral Stage-1 food-vision prompt contract;
- deterministic local confirmation derivation for mixed plates, sauces, low confidence, warnings, uncertainty, missing weights, broad ranges, and ambiguous normalization;
- provider-free replay coverage for prompt neutrality and local ambiguity calibration.

Changed:
- updated the CURRENT_STATE verification base to project main `0e176d0bc8db06d0443be049aa62855ebed9db51`;
- reduced benchmark-specific anchoring in the prompt and moved confirmation decisions out of prompt wording into local application logic;
- kept historical R7D-B benchmark evidence unchanged while clarifying that Qwen live quality is not yet revalidated and Gemini compatibility remains unproven.

Safety:
- provider requests during validation: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged;
- strict schema validation, aggregate nutrition rejection, retry=0, and fallback=0 remained unchanged.


## 1.2.6 - 2026-07-09

Added:
- sanitized Gemini failure diagnostic contract with allowlisted execution stages and categories;
- provider-free Gemini request-shape compatibility coverage for the native adapter path;
- provider-free classification tests for wrapped HTTP, transport, decode, content-extraction and inventory-validation failures.

Changed:
- updated the CURRENT_STATE verification base to project main `7b38d862978781b711b1ca5d76e1735bc7ee0d27`;
- preserved typed Gemini stage/category metadata before redacting raw provider details;
- kept historical Gemini benchmark evidence unchanged as `GEMINI_UNKNOWN_OPERATIONAL_FAILURE`;
- clarified that future Gemini retests may yield narrower safe categories, but live Gemini compatibility remains unproven.

Safety:
- provider requests during validation: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged;
- raw provider errors, raw responses, keys and image payloads remained redacted.


## 1.2.5 - 2026-07-09

Added:
- recorded the exact-main Stage-1 food vision provider benchmark against three approved sanitized assets;
- recorded exact benchmark request accounting and the audit image digest;
- recorded provider classifications for Gemini and Qwen from the same offline benchmark.

Changed:
- updated the CURRENT_STATE verification base to project main `10543bf2ad05c518f202eb23bc52fcd45dfa25e6`;
- promoted the benchmark result to the top active blocker because no provider met the Stage-1 rollout gate;
- updated the active-work section from R7C implementation state to R7D-B benchmark state.

Safety:
- provider requests during validation: 6 total (3 Gemini, 3 Qwen), with 0 retries, 0 fallbacks and 0 repair requests;
- Telegram requests, diary writes, production DB opens/writes and Qdrant requests remained 0;
- production build/deploy/restart not performed and production runtime remained unchanged;
- no secrets, raw provider responses or raw provider errors were stored.

## 1.2.4 - 2026-07-09

Added:
- local component-confirmation flow for mixed-plate meal photos;
- explicit correction commands for component replacement, addition, removal and weight confirmation;
- focused regression coverage for Stage-1 inventory confirmation and Stage-2 safe nutrition handoff.

Changed:
- split meal-photo confirmation into inventory confirmation first and diary-save confirmation second;
- blocked diary save until nutrition is derived only from confirmed components;
- updated the CURRENT_STATE verification base to project main `b1d540bb40e93e8ec56ab41e02c0bacfebd566d0`.

Safety:
- provider requests during validation: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.3 - 2026-07-09

Added:
- component-grounded Stage-1 visual inventory contract for meal-photo analysis;
- strict local validator for Stage-1 vision output;
- offline mixed-plate quality fixtures and thresholds.

Changed:
- rejected model-generated aggregate nutrition from the vision path;
- blocked low-confidence or invalid vision output from staging a diary-ready pending meal;
- updated the CURRENT_STATE verification base to project main `4aa67def8b4ece2aab6bb0ebdeb121318ccc7eab`.

Safety:
- provider requests during validation: 0;
- production remains on Gemini;
- Qwen remains not deployed and not active in production;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.2 — 2026-07-09

Added:
- recorded the exact-main Qwen live activation attempt outcome;
- recorded that synthetic probe succeeded while live Telegram food recognition quality failed;
- recorded the clean rollback to the previous Gemini production image;
- recorded the R7A forensic classification and remediation-only next step.

Changed:
- updated the CURRENT_STATE verification base to project main `22ed9e4d103b192947902fb66d6ad633b4d3ee31`;
- changed Qwen state from implemented and not yet deployed to activation attempted, rejected on quality, rolled back;
- clarified that current production routing is back on Gemini and Qwen is not active in production.

Safety:
- no provider requests were performed in this tracked-change step;
- no production config, build, deploy or restart actions were performed in this tracked-change step;
- production DB remained unchanged for this tracked-change step;
- Qdrant remained unchanged;
- no secrets, private IDs, raw provider responses or raw Telegram artifacts were stored.

## 1.2.1 — 2026-07-09

Added:
- verified external access for `qwen3-vl-8b-instruct`;
- recorded confirmed image-understanding success for the verified Qwen3 model;
- recorded confirmed `qwen2.5-vl-7b-instruct` model-specific `access_denied`.

Changed:
- corrected the tracked Qwen vision target model identifier to `qwen3-vl-8b-instruct`;
- updated the CURRENT_STATE verification base to project main `3cac5ecf6b47671d57675f2c26995d5ab97370f1`;
- clarified that credential replacement is not required and production remains on Gemini.

Safety:
- production unchanged;
- build/deploy/restart not performed;
- provider requests and Telegram smoke not performed in this tracked-change step;
- DB/Qdrant unchanged;
- no secrets, private IDs or raw provider responses stored.

## 1.2.0 — 2026-07-08

Added:
- current-main Qwen vision integration state;
- source and integration implementation commit identifiers;
- task-scoped `QWEN_API_KEY` routing status;
- local validation results for focused, related and agent-check suites.

Changed:
- V2-R1 status moved from in-progress to implemented and locally validated;
- CURRENT_STATE verification base moved to project main `60f84093c0fe82d29814c2ac8e3c0fb6dc847e7b`;
- next allowed V2-R1 sequence is expressed without temporary Draft PR state so it remains true after merge.

Safety:
- Qwen remains not deployed and not active in production;
- production build/deploy/restart not performed;
- provider requests and Telegram smoke not performed;
- DB/Qdrant unchanged;
- no secrets, private IDs or raw provider responses stored.

## 1.1.0 — 2026-07-08

Added:
- completed Gemini reason probe;
- Gemini Developer API/auth confirmation;
- ListModels 403 result;
- active V2-R1 Qwen worktree;
- call_policy defect;
- separation of implementation/merge/deploy/smoke.

Changed:
- old V2 base mismatch moved to history;
- Gemini next step changed to Google Console audit;
- Qwen explicitly marked not deployed.
- clarified that the recorded SHA is the state-verification base, not a self-referential future main HEAD.

Safety:
- production unchanged;
- build/deploy not performed;
- DB/Qdrant unchanged;
- no secrets stored.

## 1.0.0 — 2026-07-08

- initial source-of-truth file created.

## v1.2.59
- Implemented Persistent Derived Graph Storage (PR-3).
- Fixed GraphSnapshot.create determinism bug.
- Corrected CURRENT_STATE header version.
- Added ADR-0083.

## 2026-08-19
Moved PR-5 foundation merged state from CURRENT_STATE.md to changelog.
PR #201: FOUNDATION_MERGED, EXACT_HEAD_TESTS=FAIL
PR #202: PARTIAL_CLOSURE_MERGED, EXACT_HEAD_TESTS=FAIL, ADMIN_MERGE_USED=true

PR #203:
FULL_CONTRACT_MERGED
EXACT_HEAD_TESTS=PASS
EXACT_HEAD_TECHNICAL_CI=PASS
ADMIN_MERGE_USED=false
AUTO_MERGE_USED=false
MERGE_SHA=575a0bb854c46c0425db4d864b55f9539899679a

PR-6: Deterministic Memory Graph Retrieval & Convergence Evals implementation candidate.

## Memory Graph V3 (PR-6.1)
- Added PR-6.1 candidate status to CURRENT_STATE.md.

## 2026-08-22 — Memory Graph V3 PR-6.1 closure
- PR #204 merged by ordinary squash commit `db9620284cac8e1fe6af6c8420a1dfbf8194a557` after exact-head CI passed.
- Exact-main deterministic corpus validation passed twice: candidate digest `6c5df3e4f152cbc48d3b674a37ed5935a62bd327ca1eda2790d9770d9ad92bda`; reports and report IDs were byte-identical.
- Same-revision F5 semantic tamper is closed as a hard graph-read integrity failure; graph runtime and production remain inactive and unchanged.

## 2026-08-22 - Memory Graph V3 PR-7 closure
- PR #206 exact-head CI PASS;
- squash merge SHA e60b2af92226eae0d14c79ca382629613a387e39;
- exact-main validation PASS;
- runtime integration repository-only;
- default mode disabled;
- shadow capability available but inactive;
- serve unavailable;
- no production/runtime activation.
