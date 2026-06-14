---
name: coding-loop
description: Iterative coding loop for Hermes/HealBite: plan, edit, test, review, stop safely, and save reusable lessons.
version: 1.0.0
metadata:
  hermes:
    tags: [coding, loop, verification, healbite, hermes]
    category: dev
    requires_toolsets: [terminal, file, memory, session_search, todo]
---

# Coding Loop

## When to Use
Use this skill for code changes, bug fixes, refactors, migrations, tests, deploy preparation, and PR/checkpoint work in Hermes/HealBite.

## Project Rules
Before making changes, read:
- AGENTS.md
- rulebook.md
- MEMORY.md
- USER.md
- SOUL.md
- RUNBOOK_MEMORY_OS.md, if relevant

Respect the current architecture:
- SQLite is the only Source of Truth.
- Qdrant is a disposable semantic index.
- Telegram polling belongs to Hermes Gateway.
- Do not start a second Telegram polling process.
- Do not hardcode Telegram user IDs.
- Do not expose provider/auth errors to users.
- Do not change secrets, LLM provider/model, admin-list, Qdrant toggle, or production env without explicit approval.

## Loop Contract
You are not doing single-shot prompting. You are running a bounded engineering loop.
Each iteration must follow:
1. Restate the task and success criteria.
2. Inspect only the files needed for the next decision.
3. Make the smallest coherent change.
4. Run verification.
5. Decide:
   - continue if verification reveals a clear next fix;
   - stop if success criteria pass;
   - stop and ask the user if requirements are ambiguous;
   - stop if no progress is made for 2 consecutive iterations.

## Hard Limits
- Max iterations: 6.
- Max broad repo scans: 2.
- Do not rewrite unrelated files.
- Do not run destructive commands without explicit approval.
- Do not run git reset --hard.
- Do not delete backups.
- Do not hide failing tests.
- Do not commit secrets.
- Do not print token/password values.

## Verification
If scripts/agent_check.sh exists, run it after every code-changing iteration.
Otherwise prefer:
- py_compile for changed Python files;
- ruff check and ruff format for changed Python files;
- targeted pytest near changed files;
- docker compose build/restart only when needed;
- docker logs after deploy.

## Deploy Rules
Do not deploy automatically unless the user explicitly asks for deploy.
Before deploy:
1. Create or confirm backup.
2. Run targeted tests.
3. Rebuild container only if code changed.
4. Check:
   - hermes-bot running
   - restart_count=0
   - logs have no traceback
   - no database is locked
   - no Provider authentication failed in user-facing path

## Memory Update
At the end:
- Summarize what changed.
- Summarize verification result.
- Save stable reusable project conventions to memory.
- Do not save temporary debugging noise as long-term memory.

## Final Report
Use this format:
### Task
### Changes
### Verification
### Risks
### Next Step
