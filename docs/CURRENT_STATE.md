---
title: Hermes / HealBite — Current State
version: 1.2.1
updated_at: 2026-07-09
status: active
source_of_truth: true
state_verified_against_main_sha: 3cac5ecf6b47671d57675f2c26995d5ab97370f1
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## 1. Summary

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Project state in this document was verified against HealBite main SHA:
  `3cac5ecf6b47671d57675f2c26995d5ab97370f1`.
- The local `origin` remote points to upstream `NousResearch/hermes-agent` and
  is not the HealBite project remote.
- Canonical checkout: `/home/hermes/.hermes/hermes-agent`.
- Canonical checkout state during this update: dirty; it must not be cleaned,
  reset, stashed or modified by unrelated tasks.
- Last confirmed `hermes-bot` runtime: running, restart count 0.
- Last confirmed Qdrant runtime: running, restart count 0.
- Qdrant has not been intentionally changed by recent HealBite rollout steps.
- Production git SHA: `unknown`.
- Production image digest: observed by Docker inspect in earlier rollout work,
  but not mapped to a source SHA by this document.
- Qwen vision implementation is present in this repository state and validated,
  but not deployed or active in production.
- Weekly/shopping production feature flags: last confirmed target state is
  feature-disabled for shopping and allowlisted for weekly, but effective
  runtime config must be re-confirmed before any rollout decision.

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

### P0 — Gemini vision HTTP 403

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

### P1 — Existing weekly-menu draft cannot be published

Confirmed state from the last review report: the draft had 20 entries instead of 21, Sunday dinner was missing, review output contained replacement-question-mark encoding defects, and technical enum slot names appeared in user-facing review output. The draft must stay hidden with `publish=false` and `automatic_regeneration=false`. Before publication, validation must require 3 meal slots for each of 7 days, Unicode rendering must be fixed, enum labels must stay internal, and a separate publish approval is required.

### P2 — CI technical debt

Known state: six Telegram parse-mode failures match the existing baseline. They are not a new regression for this state update, but still require a fix or quarantine with owner and deadline.

## 4. Active Work — Sprint 7.1V2-R1

Status:

`STATUS=IMPLEMENTED_LOCAL_VALIDATED_NOT_DEPLOYED`

Worktrees:

- Source worktree:
  `/home/hermes/.hermes/worktrees/healbite-s71v2-qwen-primary-vision`.
- Integration worktree:
  `/home/hermes/.hermes/worktrees/healbite-s71v2r1-qwen-current-main`.

Confirmed state:

- Original V2 base:
  `20f1469dc395130fbde30b9736750e247e9b8306`.
- Current project main base:
  `3cac5ecf6b47671d57675f2c26995d5ab97370f1`.
- Main advancement from the old V2 base was docs-only:
  `AGENTS.md`, `docs/CURRENT_STATE.md`, and
  `docs/CURRENT_STATE_CHANGELOG.md`.
- Source implementation commit:
  `fe25fde2ec45ea284b1bdd21954df4356640800a`.
- Integration implementation commit:
  `797ce97ca516ce624a4ccd3b1247a96bd2b9f207`.
- Canonical dirty checkout was not modified by the V2-R1 work.
- Goal: Qwen3-VL as primary vision provider through task-scoped auxiliary
  vision routing.
- Integration shape: OpenAI-compatible vision configuration with task-scoped
  key resolution.
- Target provider value: `openai`.
- Target model value: `qwen3-vl-8b-instruct`.
- Target base URL:
  `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`.
- Target API key env var: `QWEN_API_KEY`.
- Same key, Singapore endpoint, workspace and auth method were externally
  verified across both tested Qwen model identifiers.
- Verified accessible vision model: `qwen3-vl-8b-instruct` (HTTP 200, image
  understanding confirmed).
- Verified denied vision model: `qwen2.5-vl-7b-instruct` (HTTP 403,
  `access_denied`).
- Credential replacement is not required for the production activation path.
- Text, weekly, shopping, memory, Qdrant and Telegram routing are unchanged by
  the implementation diff.
- One provider request is allowed per one vision turn.
- No Qwen-to-Gemini fallback is allowed for vision.
- Production activation requires a separate deploy-gate stage.

Local validation completed on the integration branch:

- `python -m py_compile` for changed Python files: PASS.
- Focused Qwen/task-scoped vision tests: PASS, 7 tests.
- Related provider/vision/gateway regression suite: PASS, 386 tests.
- `scripts/secret_check.sh`: PASS.
- `scripts/agent_check.sh`: PASS, 510 targeted tests.
- `git diff --check`: PASS.

Not performed:

- Merge.
- Build.
- Deploy.
- Production restart or container recreate.
- Production DB write.
- Qdrant mutation.
- Provider request.
- Telegram smoke.

Final V2-R1 state line:

Qwen vision support is implemented and locally validated in the integration branch, the verified target model is `qwen3-vl-8b-instruct`, and it is not deployed or active in production.

## 5. Previous V2 Attempt History

- The original V2 attempt was aborted.
- Old approved base:
  `d80526905135dbcf6df2f034fdfcd51463a889a3`.
- Project remote main at abort time:
  `20f1469dc395130fbde30b9736750e247e9b8306`.
- No branch, worktree or patch was created for that obsolete base.
- The blocker was closed by the updated V2-R1 playbook.

## 6. Next Allowed Sequence

For V2-R1:

1. If the integration PR is not yet merged, complete CI, review, and merge gate.
2. Build, deploy and runtime activation only in a separate approved stage.
3. After successful deploy, run exactly one controlled Telegram smoke.

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

Before any production deployment decision, re-confirm: production source SHA, image-digest-to-source mapping, effective feature flags, final Qwen/DashScope-compatible base URL, authoritative runtime config location, final V2-R1 CI status, Google Console audit result, and whether the current production DB has all weekly/shopping tables.

## 9. Update Rules

- Update this file in the same PR that changes confirmed project state.
- Use patch/minor/major versioning.
- Move superseded state into `docs/CURRENT_STATE_CHANGELOG.md`.
- Never store secrets, credentials, private identifiers or raw provider responses
  in this file.
- Mark unverifiable facts as `UNKNOWN` or `NOT CONFIRMED`.
