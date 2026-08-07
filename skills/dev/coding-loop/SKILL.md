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

Load the domain procedure when the loop reaches operational work:
- deployment, backup, migration, or rollback: [`../../deploy/SKILL.md`](../../deploy/SKILL.md);
- SQLite/Qdrant maintenance: [`../../memory/SKILL.md`](../../memory/SKILL.md);
- Telegram runtime debugging: [`../../telegram/SKILL.md`](../../telegram/SKILL.md).

Keep repository-wide safety and architecture rules in `AGENTS.md`; do not duplicate them here.

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
- post-deploy verification from the deployment skill when deployment is authorized.

## Deploy Rules
Use [`../../deploy/SKILL.md`](../../deploy/SKILL.md). A coding-loop success never authorizes production mutation by itself.

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
