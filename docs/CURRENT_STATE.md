---
title: Hermes / HealBite — Current State
version: 1.2.8
updated_at: 2026-07-09
status: active
source_of_truth: true
state_verified_against_main_sha: 14981980403da56db94c90483bcab4ee209e9784
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## 1. Summary

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Project state in this document was verified against HealBite main SHA:
  `14981980403da56db94c90483bcab4ee209e9784`.
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
- Limited exact-main vision re-benchmark evidence was recorded against approved
  main SHA `14981980403da56db94c90483bcab4ee209e9784`.
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
- Limited re-benchmark request accounting stayed within the approved cap:
  4 total provider requests, 1 Gemini request, 3 Qwen requests, 0 retries,
  0 fallbacks, 0 repair requests, 0 Telegram requests, 0 production DB opens,
  0 production DB writes, and 0 Qdrant requests.
- Gemini used asset `02_simple_plate.jpg`, reached request construction and the
  live provider HTTP step, and failed as `GEMINI_ACCESS_DENIED` with HTTP class
  `4xx` before decode, extraction or validation; Gemini schema validity,
  validator reach and food-quality evaluation remain not established by the
  limited re-benchmark.
- Repository code preserves sanitized Gemini execution-stage and category
  diagnostics for future provider-free validation and any separately approved
  external-auth follow-up.
- Qwen completed 3/3 schema-valid benchmark responses on
  `qwen3-vl-8b-instruct`, but classified as `QWEN_FAIL_CLOSED_COMPATIBLE`
  because food-quality and ambiguity gates were not met.
- Qwen limited re-benchmark metrics were: major-component precision `0.625`,
  major-component recall `0.600`, sauce recall `0.000`, and confirmation
  correctness `1.000`; the ambiguity gate still failed.
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

### P0 — Limited re-benchmark confirmed no rollout-eligible provider

Confirmed state:

- Approved/current main for the limited re-benchmark:
  `14981980403da56db94c90483bcab4ee209e9784`.
- Benchmark assets remained the three approved sanitized images with SHA256
  `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Limited re-benchmark request accounting stayed within the approved hard cap:
  4 total provider requests, 1 Gemini request, 3 Qwen requests, 0 retries,
  0 fallbacks, 0 repair requests, 0 Telegram requests, 0 production DB opens,
  0 production DB writes, and 0 Qdrant requests.
- Gemini was exercised exactly once on `02_simple_plate.jpg` and failed at
  `PROVIDER_HTTP` as `GEMINI_ACCESS_DENIED` with HTTP class `4xx`.
- Gemini schema validity, validator reach, and food-quality evaluation remain
  unproven because the limited re-benchmark never reached decode or validation.
- Qwen completed 3/3 schema-valid outputs on `qwen3-vl-8b-instruct`.
- Qwen limited re-benchmark metrics were major-component precision `0.625`,
  major-component recall `0.600`, sauce recall `0.000`, and confirmation
  correctness `1.000`.
- Local safety remediation worked: schema validity remained `3/3`, validator
  pass remained `3/3`, aggregate nutrition violations remained `0`, invalid
  staging remained `0`, and no unsafe diary staging was observed, but
  recognition quality remained below the rollout threshold.
- Qwen remained `QWEN_FAIL_CLOSED_COMPATIBLE`; the ambiguity gate still failed,
  `benchmark_candidate=false`, and Qwen remained ineligible for rollout.
- This three-image limited re-benchmark is insufficient to establish general
  model quality improvement or regression; it is bounded evidence for current
  rollout eligibility only.
- No eligible provider was produced, automatic provider selection remained
  false, deployment remained unauthorized, and deployment remained blocked.
- Raw provider responses were not stored; secret leakage remained false; raw
  error leakage remained false.
- Production runtime remained unchanged during the limited re-benchmark.

Current verdict:

`V2-R7E-C1 BLOCKED — GEMINI ACCESS DENIED — QWEN REMAINS FAIL-CLOSED AND INELIGIBLE — NO PROVIDER ELIGIBLE FOR ROLLOUT — PRODUCTION UNCHANGED`

Next vision step:

- Keep production on the existing Gemini deployment state until a separately
  approved provider path is proven eligible.
- Do not run a new live Telegram photo smoke until a provider earns an offline
  PASS and a fresh activation playbook is approved.

Evidence:

- `/home/hermes/evidence/s71v2-r7e-c1-limited-rebenchmark/20260709T134049Z/summary.json`
- `/home/hermes/evidence/s71v2-r7e-c1-limited-rebenchmark/20260709T134049Z/eligibility_decision.md`
- `/home/hermes/evidence/s71v2-r7e-c1-limited-rebenchmark/20260709T134049Z/historical_comparison.md`

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

## 4. Active Work - Sprint 7.1V2-R7E-C1-DOCS

Status:

`STATUS=LIMITED_VISION_REBENCHMARK_RECORDED_NO_PROVIDER_ELIGIBLE_DEPLOYMENT_BLOCKED_PRODUCTION_UNCHANGED`

Current recorded state:

- Approved current main source for this state update:
  `14981980403da56db94c90483bcab4ee209e9784`.
- Limited re-benchmark evidence path:
  `/home/hermes/evidence/s71v2-r7e-c1-limited-rebenchmark/20260709T134049Z`.
- Benchmark assets and manifest matched the previously approved benchmark set.
- Gemini was exercised once and failed at `PROVIDER_HTTP` as
  `GEMINI_ACCESS_DENIED` with HTTP class `4xx`.
- Qwen was exercised three times on `qwen3-vl-8b-instruct`, produced 3/3
  schema-valid responses, and remained `QWEN_FAIL_CLOSED_COMPATIBLE` because
  food-quality and ambiguity gates were not met.
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

1. Keep Qwen fail-closed and undeployed until a future provider revision earns a
   fresh offline PASS on approved assets.
2. Any future Qwen re-benchmark must preserve capped request accounting and must
   not bypass the ambiguity gate.
3. Do not run a new live Telegram photo smoke for Qwen until an offline PASS
   exists and a separate activation playbook is approved.

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

Before any production deployment decision, re-confirm: production source SHA, image-digest-to-source mapping, effective feature flags, final Qwen/DashScope-compatible base URL, authoritative runtime config location, final remediation PR status, Google Console audit result, and whether the current production DB has all weekly/shopping tables.

## 9. Update Rules

- Update this file in the same PR that changes confirmed project state.
- Use patch/minor/major versioning.
- Move superseded state into `docs/CURRENT_STATE_CHANGELOG.md`.
- Never store secrets, credentials, private identifiers or raw provider responses
  in this file.
- Mark unverifiable facts as `UNKNOWN` or `NOT CONFIRMED`.
