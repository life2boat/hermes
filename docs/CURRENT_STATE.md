---
title: Hermes / HealBite — Current State
version: 1.2.7
updated_at: 2026-07-09
status: active
source_of_truth: true
state_verified_against_main_sha: 0e176d0bc8db06d0443be049aa62855ebed9db51
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## 1. Summary

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Project state in this document was verified against HealBite main SHA:
  `0e176d0bc8db06d0443be049aa62855ebed9db51`.
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
- Exact-main food-vision benchmark was completed offline against approved main
  SHA `10543bf2ad05c518f202eb23bc52fcd45dfa25e6`.
- Benchmark assets were three operator-approved sanitized images with SHA256
  `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Current production vision routing is back on the prior Gemini configuration.
- Qwen vision code is present in main, but Qwen is not deployed or active in
  production after the rejected live activation.
- A component-grounded Stage-1 food vision contract is implemented in repository
  code and has passed offline-only validation so far.
- Stage-1 vision output now rejects model-generated aggregate calories/macros
  and cannot stage a diary-ready pending meal directly from a photo result.
- Mixed-plate photo flow now uses a two-step component confirmation path:
  Stage-1 confirms visible components first, Stage-2 calculates nutrition only
  from confirmed components and then asks for the final diary save decision.
- User correction commands for meal-photo components are implemented locally
  (confirm/cancel/replace/add/remove/weight) without generic-agent handoff.
- Offline mixed-plate food-vision quality fixtures and deterministic thresholds
  are present in the test suite.
- The Stage-1 food-vision prompt is now shorter, provider-neutral, and no
  longer anchored to the failed benchmark plate or pastry labels.
- Local confirmation requirement is now derived deterministically from validated
  inventory data and cannot be suppressed by provider `needs_user_confirmation=false`.
- Mixed plates, sauces, low confidence, uncertainty, warnings, missing weights,
  broad ranges, and over-specific normalization now force clarification locally.
- Historical R7D-B benchmark results remain unchanged; Qwen quality has not yet
  been revalidated live or rebenchmarked after this remediation.
- Historical Gemini benchmark remains recorded as
  `GEMINI_UNKNOWN_OPERATIONAL_FAILURE`.
- Repository code now preserves sanitized Gemini execution-stage and category
  diagnostics for future provider-free validation and any separately approved
  live retest.
- Qwen completed 3/3 schema-valid benchmark responses, but classified as
  `FAIL_CLOSED_COMPATIBLE` because food-quality and ambiguity gates were not met.
- No benchmarked provider is eligible for rollout, and no automatic selection
  was performed.
- Benchmark request accounting stayed within the approved cap: 6 total provider
  requests, 3 Gemini, 3 Qwen, 0 retries, 0 fallbacks, 0 repair requests,
  0 Telegram requests, 0 production DB opens/writes, and 0 Qdrant requests.
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

### P0 — No provider met the Stage-1 food vision benchmark gate

Confirmed state:

- Approved/current main for the audited benchmark:
  `10543bf2ad05c518f202eb23bc52fcd45dfa25e6`.
- Exact audit image used for the benchmark:
  `sha256:556985acd3eb46f2b8d673d529a304a5723dca67a25a195c62e5293d12953de8`.
- Benchmark assets were the three approved sanitized images with SHA256
  `135872354b6c531fdeeb4cdabf2b3edfddc62d943f944b8a8600aad3806ebd74`,
  `6b06b7f5bc822ac2d806472840f41be58dad4d2cce472c113d7b3487fbc1ed8d`, and
  `58a4b4a12c19deeafa12be55e965300ed89eb57aa1adecea1daa323204379363`.
- Benchmark request accounting stayed within the approved hard cap:
  6 total provider requests, 3 Gemini requests, 3 Qwen requests, 0 retries,
  0 fallbacks, 0 repair requests, 0 Telegram requests, 0 production DB opens,
  0 production DB writes, and 0 Qdrant requests.
- Gemini completed 0/3 responses, produced 0/3 schema-valid outputs, and remains
  historically classified as `GEMINI_UNKNOWN_OPERATIONAL_FAILURE` for the stored
  benchmark evidence.
- Qwen completed 3/3 schema-valid outputs, but only reached major-component
  precision `0.5556`, major-component recall `0.625`, and ambiguous
  confirmation correctness `0.0`; classification:
  `FAIL_CLOSED_COMPATIBLE`.
- No provider met the Stage-1 acceptance gates, no eligible provider was
  produced, and no automatic provider selection was performed.
- Raw provider responses were not stored; secret leakage remained false; raw
  error leakage remained false.
- Production runtime remained unchanged during the benchmark.

Current verdict:

`V2-R7D-B BLOCKED — NO PROVIDER MET THE STAGE-1 SCHEMA AND FOOD QUALITY ACCEPTANCE GATES — NO DEPLOYMENT AUTHORIZED — PRODUCTION UNCHANGED`

Next vision step:

- Complete provider-specific remediation before any new production activation
  request.
- Do not run a new live Telegram photo smoke until an offline benchmark PASS
  exists.

Evidence:

- `/home/hermes/evidence/s71v2-r7d-provider-benchmark/20260709T095936Z/summary.json`
- `/home/hermes/evidence/s71v2-r7d-provider-benchmark/20260709T095936Z/provider_comparison.md`

### P1 — Gemini vision HTTP 403

Confirmed state:

- API family: `gemini_developer_api`.
- Authentication mechanism: API key header.
- Header used by runtime: `x-goog-api-key`.
- Authoritative credential source: runtime callable provider.
- Endpoint/auth family match: true.
- Runtime key resolution defect was fixed in PR48.
- Gemini vision still returned HTTP 403 in the live validation evidence.
- Safe reason, domain and canonical status were not present in the stored
  provider error evidence.
- Successful Gemini text calls with the same credential source are not proven.
- One approved reason probe has already been used and is exhausted.
- Repeating the same Gemini probe is not allowed without a separate decision.

Additional ListModels result recorded from operator workflow:

- Query-parameter API key mode returned HTTP 403 with `text/html`.
- `x-goog-api-key` header mode returned HTTP 403 with `text/html`.
- No Gemini model list was obtained.
- Configured model remains `gemini-2.5-flash`.
- Do not change the Gemini model name only as a naming fix.

Current verdict:

`V1-R4 INCONCLUSIVE — APPROVED REASON PROBE EXHAUSTED — EXTERNAL GOOGLE CONSOLE AUDIT REQUIRED — PRODUCTION UNCHANGED`

Evidence:

- `/home/hermes/evidence/s71v1-r4/20260707T135028Z/summary.json`
- `/home/hermes/evidence/s71v1-r3b-live/20260707T002132Z/summary.json`

Next Gemini step:

- Operator-only read-only Google Console audit.

Do not perform until separately authorized: additional Gemini diagnostics, reason probes, credential rotation, new-key creation, production config changes or Telegram photo smoke for Gemini.

### P2 — Existing weekly-menu draft cannot be published

Confirmed state from the last review report: the draft had 20 entries instead of 21, Sunday dinner was missing, review output contained replacement-question-mark encoding defects, and technical enum slot names appeared in user-facing review output. The draft must stay hidden with `publish=false` and `automatic_regeneration=false`. Before publication, validation must require 3 meal slots for each of 7 days, Unicode rendering must be fixed, enum labels must stay internal, and a separate publish approval is required.

### P3 — CI technical debt

Known state: six Telegram parse-mode failures match the existing baseline. They are not a new regression for this state update, but still require a fix or quarantine with owner and deadline.

## 4. Active Work - Sprint 7.1V2-R7E-B2

Status:

`STATUS=QWEN_FOOD_GROUNDING_PROMPT_SIMPLIFIED_LOCAL_AMBIGUITY_CALIBRATION_HARDENED_PROVIDER_FREE_VALIDATION_COMPLETE_PRODUCTION_UNCHANGED`

Current remediation state:

- Approved current main source for this state update:
  `0e176d0bc8db06d0443be049aa62855ebed9db51`.
- Historical benchmark source remains:
  `10543bf2ad05c518f202eb23bc52fcd45dfa25e6`.
- Stage-1 prompt was simplified and benchmark-specific anchoring was reduced.
- Confirmation requirement is now derived locally from validated inventory data.
- Provider `needs_user_confirmation=false` can no longer suppress local caution.
- Mixed plates, sauces, low confidence, uncertainty, warnings, missing weight
  ranges, broad ranges, and ambiguous normalization now force clarification.
- Strict schema validation, aggregate nutrition rejection, malformed JSON
  rejection, two-phase confirmation, retry=0, and fallback=0 remain unchanged.
- Historical R7D-B benchmark evidence and recorded scores were not modified.
- Qwen quality has not yet been rebenchmarked after this remediation.
- Gemini compatibility remains unproven and Gemini diagnostics behavior from
  R7E-B1 remains unchanged.
- Provider requests: 0.
- Telegram requests: 0.
- Production DB opens/writes: 0 / 0.
- Qdrant requests: 0.
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

1. Improve component grounding quality until offline benchmark precision/recall
   and ambiguity thresholds are met.
2. Re-run the offline benchmark with approved assets and capped request
   accounting before any new deploy request.
3. Do not run a new live Telegram photo smoke until an offline benchmark PASS
   exists.

For Gemini:

- Do not run new diagnostics.
- Wait for Google Console audit or a separate operator decision.
- Do not treat the benchmark operational failure as sufficient to change the
  external-auth remediation path by itself.

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
