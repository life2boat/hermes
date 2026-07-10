---
title: Hermes / HealBite — Current State
version: 1.2.10
updated_at: 2026-07-10
status: active
source_of_truth: true
state_verified_against_main_sha: 1e048a7479253283ba2087e4e2ef6ad9ca584556
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## 1. Summary

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Project state in this document was verified against HealBite main SHA:
  `1e048a7479253283ba2087e4e2ef6ad9ca584556`.
- This verification SHA records repository state and Source-of-Truth docs closure
  only; it does not identify a deployed production revision.
- The local `origin` remote points to upstream `NousResearch/hermes-agent` and
  is not the HealBite project remote.
- Canonical checkout: `/home/hermes/.hermes/hermes-agent`.
- Canonical checkout state during this update: dirty; it must not be cleaned,
  reset, stashed or modified by unrelated tasks.
- Last confirmed `hermes-bot` runtime: running, restart count 0.
- Last confirmed Qdrant runtime: running, restart count 0.
- Qdrant has not been intentionally changed by recent HealBite rollout steps.
- Production git SHA: `unknown`.
- Production image digest remains unmapped to a source SHA by this document.
- Next-generation Qwen access and benchmark evidence remains anchored to the
  benchmarked repository main SHA `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`.
- That Q1 evidence used repository food-vision prompt/validator/confirmation
  components together with a task-scoped DashScope-compatible benchmark
  harness, not the current built-in Hermes Qwen OAuth runtime.
- Benchmark assets remained the same three operator-approved sanitized images
  with SHA256 `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Current production vision routing remains on the previously deployed Gemini
  configuration by deployment state only; this docs task did not change
  production config or runtime.
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
- The Stage-1 food-vision prompt remains shorter, provider-neutral, and no
  longer anchored to the failed benchmark plate or pastry labels.
- Local confirmation requirement is derived deterministically from validated
  inventory data and cannot be suppressed by provider `needs_user_confirmation=false`.
- Mixed plates, sauces, low confidence, uncertainty, warnings, missing weights,
  broad ranges, and over-specific normalization force clarification locally.
- Next-generation Qwen request accounting stayed within the approved cap:
  6 total provider requests, 3 access probes, 3 benchmark requests, 0 retries,
  0 fallbacks, 0 repair requests, 0 Telegram requests, 0 production DB opens,
  0 production DB writes, and 0 Qdrant requests.
- All three next-generation aliases `qwen3.7-plus`, `qwen3.6-plus`, and
  `qwen3.6-flash` were operationally reachable on the access asset within that
  task-scoped external benchmark context, each produced schema-valid inventory
  output, and each passed the local validator.
- Access/schema success in that external benchmark context is not a
  food-quality benchmark and does not make any tested alias rollout eligible.
- Q1 model access and benchmark results remain valid for the recorded model
  aliases, but Q1 did not validate the current built-in Hermes Qwen OAuth
  runtime and did not prove deployable Qwen provider integration.
- `qwen3.7-plus` was selected as the highest-priority model under the
  pre-approved fixed ordering among models that passed the access/schema probe.
- Only `qwen3.7-plus` received the full three-image benchmark in this task;
  `qwen3.6-plus` and `qwen3.6-flash` were not quality-benchmarked.
- `qwen3.7-plus` completed 3/3 schema-valid benchmark responses and 3/3 local
  validator passes, with aggregate nutrition violations `0`, invalid staging
  `0`, confirmation correctness `1.000`, and ambiguity gate pass `true`.
- `qwen3.7-plus` food grounding remained below the approved quality gate:
  major-component precision `0.111111`, major-component recall `0.444444`, and
  sauce recall `0.5`.
- `qwen3.7-plus` was classified as `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE` and
  `benchmark_candidate=false`.
- Successful schema output did not imply acceptable recognition quality.
- Relative to the previous `qwen3-vl-8b-instruct` benchmark, mixed-plate and
  simple-plate component precision/recall regressed, aggregate sauce recall
  remained below the gate, confirmation correctness remained `1.0`, schema
  compatibility remained valid, ambiguity handling improved, and invalid
  staging remained zero.
- The three-image benchmark remains a release gate only and is too small to
  establish general superiority of one model over another.
- No provider is eligible for rollout, no automatic provider selection was
  performed, deployment remains unauthorized, and deployment remains blocked.
- Weekly/shopping production feature flags: last confirmed target state is
  feature-disabled for shopping and allowlisted for weekly, but effective
  runtime config must be re-confirmed before any new rollout decision.

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
- User isolation and fail-closed cleanup are project invariants.
- Weight and water tracking have passed previous controlled production smokes.
- Weekly menu backend mutation and validated draft generation were merged in PR43.
- Weekly menu backend merge commit:
  `31f2594d2de352db3c0c6c78513770bdf5c606ab`.
- Production deployment state for the weekly backend is not confirmed by this
  document.
- Shopping runtime remains disabled unless a later state update proves otherwise.

## 3. Active Blockers

### P0 — Next-generation Qwen access audit confirmed no rollout-eligible provider

Confirmed state:

- Approved benchmarked main for the next-generation Qwen access audit:
  `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`.
- Current repository/docs-closure main for this correction:
  `1e048a7479253283ba2087e4e2ef6ad9ca584556`.
- Q1 execution path: `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS`.
- Q1 benchmark context: `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`.
- Q1 credential mechanism: `QWEN_API_KEY`.
- Q1 endpoint family: `DASHSCOPE_INTL`.
- Current Hermes runtime context for requested Qwen:
  `QWEN_OAUTH_PORTAL_CONTEXT`.
- Repository credential resolver used in Q1: `false`.
- Credential context match: `false`.
- Endpoint context match: `false`.
- Client path match: `false`.
- Benchmark assets remained the three approved sanitized images with SHA256
  `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Request accounting stayed within the approved hard cap:
  6 total provider requests, 3 access probes, 3 benchmark requests, 0 retries,
  0 fallbacks, 0 repair requests, 0 Telegram requests, 0 production DB opens,
  0 production DB writes, and 0 Qdrant requests.
- `qwen3.7-plus`, `qwen3.6-plus`, and `qwen3.6-flash` each produced one
  schema-valid access response on `02_simple_plate.jpg` and each passed the
  local validator within the task-scoped DashScope-compatible benchmark
  context.
- Access/schema success proved operational reachability and contract
  compatibility only within that external benchmark context; it did not prove
  food-recognition quality.
- `qwen3.7-plus` was selected as the highest-priority model under the
  pre-approved fixed ordering among models that passed the access/schema probe.
- `qwen3.7-plus` alone received the full three-image benchmark.
- `qwen3.6-plus` and `qwen3.6-flash` remain `not_benchmarked` for quality.
- `qwen3.7-plus` benchmark metrics were major-component precision `0.111111`,
  major-component recall `0.444444`, sauce recall `0.5`, and confirmation
  correctness `1.000`.
- Local safety behavior worked: schema validity remained `3/3`, validator pass
  remained `3/3`, aggregate nutrition violations remained `0`, invalid staging
  remained `0`, ambiguity handling passed, and no unsafe diary staging was
  observed.
- Food-component grounding and sauce recognition still remained below the
  rollout threshold.
- `qwen3.7-plus` was classified as `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE`,
  `benchmark_candidate=false`, and no next-generation Qwen provider became
  rollout eligible.
- Q1 benchmark results remain valid, but current Hermes Qwen runtime
  compatibility remains unproven and no deployable Qwen integration became
  proven.
- The three-image benchmark is a bounded release gate and is too small to
  establish general superiority of one model over another.
- No eligible provider was produced, automatic provider selection remained
  false, deployment remained unauthorized, and deployment remained blocked.
- Raw provider responses were not stored; secret leakage remained false; raw
  error leakage remained false.
- Production runtime remained unchanged during the next-generation Qwen access audit.

Current verdict:

`V2-R7F-Q1 PASS — NEXT-GENERATION QWEN ACCESS AUDIT COMPLETED — STRONGEST ACCESSIBLE MODEL BENCHMARKED — MODEL REMAINS FAIL-CLOSED AND NOT ELIGIBLE — NO DEPLOYMENT AUTHORIZATION — PRODUCTION UNCHANGED`

Next vision step:

- Keep production on the existing Gemini deployment state until a separately
  approved provider path is proven eligible.
- Do not run a new live Telegram photo smoke until a provider earns an offline
  PASS and a fresh activation playbook is approved.

Evidence:

- `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/summary.json`
- `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/eligibility_decision.md`
- `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/historical_comparison.md`
- `/home/hermes/evidence/s71v2-r7f-q2-a-qwen-context-alignment/20260710T011214Z/summary.json`

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

## 4. Active Work - Sprint 7.1V2-R7F-Q2-A-DOCS

Status:

`STATUS=QWEN_BENCHMARK_CONTEXT_CLARIFIED_RESULTS_PRESERVED_RUNTIME_NOT_PROVEN_DEPLOYMENT_BLOCKED_PRODUCTION_UNCHANGED`

Current recorded state:

- Approved current main source for this docs-only state update:
  `1e048a7479253283ba2087e4e2ef6ad9ca584556`.
- Next-generation Qwen benchmark evidence remains anchored to:
  `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`.
- Context-alignment evidence path:
  `/home/hermes/evidence/s71v2-r7f-q2-a-qwen-context-alignment/20260710T011214Z`.
- Next-generation Qwen Q1 evidence path:
  `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z`.
- Recorded Q1 execution path:
  `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS`.
- Recorded Q1 benchmark context:
  `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`.
- Recorded Q1 credential mechanism:
  `QWEN_API_KEY`.
- Recorded Q1 endpoint family:
  `DASHSCOPE_INTL`.
- Current Hermes runtime context for requested Qwen remains:
  `QWEN_OAUTH_PORTAL_CONTEXT`.
- Repository credential resolver used in Q1: false.
- Benchmark assets and manifest matched the previously approved benchmark set.
- `qwen3.7-plus`, `qwen3.6-plus`, and `qwen3.6-flash` each passed the
  access/schema probe on `02_simple_plate.jpg` within the task-scoped
  DashScope-compatible benchmark context.
- Only `qwen3.7-plus` received the three-image quality benchmark.
- `qwen3.7-plus` remained `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE` because
  food-component grounding and sauce recognition stayed below the approved
  threshold.
- Q1 model benchmark results remain valid.
- Q1 current Hermes runtime proven: false.
- Q1 deployable integration proven: false.
- External harness reusable for benchmark only: true.
- Runtime alignment required before deployment: true.
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

## 6. Next Allowed Sequence

For Qwen:

1. Option A: benchmark `qwen3.6-plus` on the same three approved assets under
   the same manifest and thresholds, with a maximum future request budget of 3,
   reusing the same task-scoped DashScope context only after explicit approval.
2. Option B: benchmark `qwen3.6-flash` only after separate approval.
3. Option C: perform provider-neutral prompt/runtime research only without
   weakening the strict validator, local confirmation gate, no-macro rule, or
   two-phase save flow.
4. Recommended next quality candidate: `qwen3.6-plus`, because it passed the
   access/schema probe but has not yet received the full three-image quality
   benchmark.
5. This recommendation is a testing recommendation only and is not a
   production-provider selection.
6. Reusing the same benchmark context would establish controlled external
   benchmark-path model quality only; it would not prove current Hermes OAuth
   runtime compatibility, production integration readiness, or deployment
   authorization.
7. No repeat access probe is necessary only if the credential mechanism,
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
