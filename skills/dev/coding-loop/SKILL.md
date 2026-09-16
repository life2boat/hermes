---
name: coding-loop
description: Use for bounded Hermes code changes needing iterative validation.
version: 1.1.0
metadata:
  hermes:
    tags: [coding, loop, verification, healbite, hermes]
    category: dev
    requires_toolsets: [terminal, file, memory, session_search, todo]
---

# Bounded coding loop

Use for a code fix, narrow refactor, test addition, or reviewable implementation
that needs repeated inspect → edit → validate steps. Do not use it as a blanket
prelude to a deploy, migration, incident response, or release task; load the
applicable domain skill when that boundary is reached.

## Loop

1. State the measurable outcome and completion boundary.
2. Inspect only the source, tests, and documentation needed for the next decision.
3. Make the smallest coherent change.
4. Run focused validation and repair task-caused failures.
5. Continue while an authorized next step is clear; stop at success, a safety
   boundary, missing authority, ambiguity that changes scope, or two consecutive
   non-progress iterations.

## Context selection

Read the root `AGENTS.md` and use its router. Run `prepare_task.py` for complex
lifecycle, evidence-lineage, release/security-sensitive, or
`INTENT_CONTROL_PLANE=REQUIRED` work; do not require it for every small bounded
edit. Load deploy, memory, or Telegram procedures only for those domains.

## Validation and delivery

Choose focused tests first, then related regressions where justified. Run
`bash scripts/agent_check.sh` for code-changing work when applicable and always
run `git diff --check` before delivery. Do not hide failures or commit secrets.

Report changes, executed checks, risks, and the next authorized action. A
coding-loop success never authorizes production mutation.
