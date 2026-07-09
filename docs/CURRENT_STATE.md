---
title: Hermes / HealBite — Current State
version: 1.2.2
updated_at: 2026-07-09
status: active
source_of_truth: true
state_verified_against_main_sha: 22ed9e4d103b192947902fb66d6ad633b4d3ee31
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## 1. Summary

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Project state in this document was verified against HealBite main SHA:
  `22ed9e4d103b192947902fb66d6ad633b4d3ee31`.
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
- Exact-main Qwen vision activation was attempted, reached the normal live
  Telegram path, failed on recognition quality, and was rolled back cleanly.
- Current production vision routing is back on the prior Gemini configuration.
- Qwen vision code is present in main, but Qwen is not deployed or active in
  production after the rejected live activation.
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

### P0 — Qwen live activation failed on recognition quality

Confirmed state:

- Approved/current main for the audited rollout:
  `22ed9e4d103b192947902fb66d6ad633b4d3ee31`.
- Exact-main build and controlled production activation were executed in the
  prior approved stage.
- Synthetic Qwen probe passed with one provider request.
- One live Telegram food-photo smoke reached the normal pending-confirmation
  path but failed product quality.
- Failure class: `recognition_quality`.
- Safe operator evidence shows a composite plated meal was collapsed into an
  incorrect named dish, visible side components were not represented
  adequately, and aggregate calories/macros were not trustworthy enough for
  product acceptance.
- Meal was not saved; no second photo was sent.
- Existing evidence proves transport, authorization, request dispatch and
  structured parsing were working.
- The failed activation was rolled back successfully to the previous Gemini
  production image.
- Production DB writes remained 0 and Qdrant was unchanged.

Current verdict:

`QWEN PRODUCTION ACTIVATION REJECTED — REMEDIATION REQUIRED BEFORE ANY NEW LIVE PHOTO SMOKE`

Next Qwen step:

- Complete forensic classification and remediation design before any new live
  rollout approval.
- Do not repeat Qwen live photo smoke until offline quality gates exist.

Evidence:

- `/home/hermes/evidence/s71v2-r6-deploy/20260709T042208Z/summary.json`
- `/home/hermes/evidence/s71v2-r7a-quality-audit/20260709T044017Z/summary.json`

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

## 4. Active Work — Sprint 7.1V2-R7A

Status:

`STATUS=QWEN_ROLLED_BACK_PENDING_QUALITY_REMEDIATION`

Current rollout state:

- Exact-main deploy source:
  `22ed9e4d103b192947902fb66d6ad633b4d3ee31`.
- Qwen rollout image built successfully:
  `sha256:51dbf3952ae4ae255087f6a8aa8c756e587064e7d6a82d67a3655b9d24be7c22`.
- Previous production image restored successfully:
  `sha256:a80d58e71c5c81e5b033f1b57da82f91717f7cbc716aa95b83d6b7c2a21315ab`.
- Current production vision provider after rollback: `gemini`.
- Qwen target provider value: `openai`.
- Qwen target model value: `qwen3-vl-8b-instruct`.
- Qwen target base URL:
  `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`.
- Target API key env var: `QWEN_API_KEY`.
- Synthetic probe: PASS.
- Live Telegram smoke: FAIL on recognition quality.
- Rollback: PASS.
- Meal save after failed smoke: false.
- Second photo after failed smoke: false.
- Production DB writes: 0.
- Qdrant changes: 0.

Forensic classification summary:

- The nutrition prompt is prompt-only and does not force exhaustive visible
  component grounding for plated multi-item meals.
- The response path accepts model-provided aggregate totals directly.
- Confidence is logged but not used as a pending-stage gate.
- The pending prompt shows only the aggregate meal label/calories/macros, so
  users cannot inspect the model's detected component list before confirmation.
- Qwen transport/auth/request success therefore does not prove acceptable food
  recognition quality.

Historical implementation context from R1 remains true:

- Goal: Qwen3-VL as primary vision provider through task-scoped auxiliary
  vision routing.
- Integration shape: OpenAI-compatible vision configuration with task-scoped
  key resolution.
- Same key, Singapore endpoint, workspace and auth method were externally
  verified across both tested Qwen model identifiers.
- Verified accessible vision model: `qwen3-vl-8b-instruct` (HTTP 200, image
  understanding confirmed).
- Verified denied vision model: `qwen2.5-vl-7b-instruct` (HTTP 403,
  `access_denied`).
- Text, weekly, shopping, memory, Qdrant and Telegram routing are unchanged by
  the implementation diff.
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

1. Complete remediation design and acceptance gates from the R7A forensic step.
2. Implement prompt/schema/validation hardening in follow-up stages.
3. Rebuild and revalidate offline before any new production activation request.
4. After a future approved deploy, run exactly one controlled Telegram smoke.

For Gemini:

- Do not run new diagnostics.
- Wait for Google Console audit or a separate operator decision.

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
