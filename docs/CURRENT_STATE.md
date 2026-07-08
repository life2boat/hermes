---
title: Hermes / HealBite — Current State
version: 1.1.0
updated_at: 2026-07-08
status: active
source_of_truth: true
last_confirmed_remote_main_sha: 20f1469dc395130fbde30b9736750e247e9b8306
production_sha: unknown
---

This file is the single short operational source of truth for the current
Hermes / HealBite project state. Chat transcripts, PDFs, pasted reports and
external notes are archive/evidence only unless this file has been updated in
Git.

## 1. Summary

- Project remote: `healbite-project/main` in `life2boat/hermes`.
- Last confirmed project main SHA:
  `20f1469dc395130fbde30b9736750e247e9b8306`.
- The local `origin` remote points to upstream `NousResearch/hermes-agent` and
  is not the HealBite project remote.
- Canonical checkout: `/home/hermes/.hermes/hermes-agent`.
- Canonical checkout state during this update: dirty; it must not be cleaned,
  reset, stashed or modified by unrelated tasks.
- Last confirmed `hermes-bot` runtime: running, restart count 0.
- Last confirmed Qdrant runtime: running, restart count 0.
- Qdrant has not been intentionally changed by recent HealBite rollout steps.
- Production DB was opened read-only for this update; `PRAGMA integrity_check`
  returned `ok`.
- Production git SHA: `unknown`.
- Production image digest: observed by Docker inspect, but not mapped to a
  source SHA by this document.
- Qwen vision integration: not deployed and not active in production.
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

Known state: six Telegram parse-mode failures match the existing baseline. They are not a new regression for this docs-only state, but still require a fix or quarantine with owner and deadline.

## 4. Active Work — Sprint 7.1V2-R1

Status:

`STATUS=IN_PROGRESS_LOCAL_WORKTREE_ONLY`

Worktree:

- `/home/hermes/.hermes/worktrees/healbite-s71v2-qwen-primary-vision`

Confirmed state:

- Approved base:
  `20f1469dc395130fbde30b9736750e247e9b8306`.
- Base gate passed against `healbite-project/main`.
- Separate worktree was created.
- Canonical dirty checkout was not modified by the V2-R1 work.
- Goal: Qwen2.5-VL as primary vision provider.
- Integration shape: task-scoped OpenAI-compatible vision configuration.
- Target provider value: `openai`.
- Target model value: `qwen2.5-vl-7b-instruct`.
- Target base URL:
  `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`.
- Target API key env var: `QWEN_API_KEY`.
- Text, weekly, shopping, memory, Qdrant and Telegram routing must not change.
- One provider request is allowed per one vision turn.
- No Qwen-to-Gemini fallback is allowed for vision.
- Production activation requires a separate deploy-gate stage.

Current confirmed implementation status:

- Core diff compiled.
- Focused tests were added.
- A defect was found: `tools.vision_tools.async_call_llm` did not accept
  `call_policy`.
- A minimal fix for forwarding `call_policy` was in progress when this state
  file was created.

Not confirmed:

- Focused tests green.
- Related tests green.
- `scripts/agent_check.sh` green.
- Final diff reviewed.
- Commit created.
- Branch pushed.
- PR created.
- Merge performed.
- Build performed.
- Deploy performed.
- Qwen runtime activation performed.
- Telegram smoke performed.

Final V2-R1 state line:

Qwen is not considered integrated or working in production yet.

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

1. Finish the `call_policy` forwarding fix.
2. Run focused compile and tests.
3. Run related regression tests.
4. Run `scripts/agent_check.sh` if it does not perform external provider calls.
5. Review scope and run `git diff --check`.
6. Commit, push and open PR according to the V2-R1 playbook.
7. Build, deploy and runtime activation only in a separate approved stage.
8. After successful deploy, run exactly one controlled Telegram smoke.

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
- Keep docs-only changes separate from implementation diffs.
- Update this file in the same PR that changes confirmed project state.

## 8. Unknown Before Production Deploy

Before any production deployment decision, re-confirm: production source SHA, image-digest-to-source mapping, effective feature flags, final Qwen model identifier, final Qwen/DashScope-compatible base URL, authoritative runtime config location, final V2-R1 CI status, Google Console audit result, and whether the current production DB has all weekly/shopping tables.

## 9. Update Rules

- Update this file in the same PR that changes confirmed project state.
- Use patch/minor/major versioning.
- Move superseded state into `docs/CURRENT_STATE_CHANGELOG.md`.
- Never store secrets, credentials, private identifiers or raw provider responses
  in this file.
- Mark unverifiable facts as `UNKNOWN` or `NOT CONFIRMED`.
