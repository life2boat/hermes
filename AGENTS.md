# Hermes / HealBite Agent Instructions

## Repository identity and purpose

Hermes is a multi-surface AI-agent repository. HealBite is its product domain.
Keep application behavior, durable user data, and operational authority separate
from repository engineering evidence. For application-surface orientation (CLI,
TUI, gateway, tools, configuration, plugins, and test conventions), load
[`docs/agent-reference/HERMES_DEVELOPMENT_SURFACES.md`](docs/agent-reference/HERMES_DEVELOPMENT_SURFACES.md)
only when the changed surface requires it.

## Core architecture principles

- Make the smallest coherent in-scope change; do not refactor neighboring
  systems merely because they are nearby.
- Preserve explicit boundaries between authoritative and derived state, between
  repository evidence and runtime authority, and between a reviewed artifact
  and a mutable environment.
- Use existing executable contracts, tests, scripts, and canonical runbooks as
  authority. Do not duplicate deterministic policy in prompts or reports.
- Private artifacts, database dumps, `.env` snapshots, auth files, and raw
  memory capsules never belong in Git. Do not print secrets, private IDs,
  personal messages, raw production logs, or provider responses.

## Security and privacy invariants

- Treat credentials, user data, health data, Telegram identifiers, and Qdrant
  payloads as sensitive. Report safe classifications, hashes, and counts only.
- A passing implementation, CI job, plan, or evidence package never grants
  production, credential, database, Qdrant, or feature-activation authority.
- Preserve fail-closed behavior. `PASS` requires evidence; retain `FAIL`,
  `BLOCKED`, `NOT_RUN`, `NOT_PERFORMED`, `UNKNOWN`, and `INCONCLUSIVE` when
  appropriate.

## Autonomous execution

Proceed autonomously when an action is safe, reversible, in scope,
non-production, and does not expose secrets or delete user data. This normally
includes focused discovery, editing, tests, lint/type checks, clean worktree and
branch operations, ordinary commits, Draft PR creation, CI inspection, and
read-only Docker/SQLite inspection.

Stop for explicit direction before destructive user-data changes, unapproved
production mutation, credential mutation, force push or destructive Git/filesystem
cleanup, or an irreversible materially out-of-scope decision. Explicit task
authority covers only the named action after its stated preconditions pass.

Use a clean isolated worktree for changes. Never reset, clean, stash, rebase,
or alter a dirty canonical checkout to make it usable.

## FAST_TRACK and completion boundaries

Continue through consecutive phases when the task explicitly authorizes them
and their technical gates pass. For example, an authorized implementation may
continue through focused validation, related regressions, PR creation, CI, and
merge without an artificial review pause.

Stop when the declared completion boundary is reached; a safety gate fails;
required authority is absent; or the next action is destructive, production, or
materially out of scope. A successful phase alone neither expands authority nor
overrides a declared stop boundary.

## Production mutation boundary

Production deploys, restarts, image builds used as release proof, database
migrations/restores, Qdrant mutation, secret changes, and live Telegram sends
require explicit task authority plus the applicable procedural skill. Do not
infer that authority from a local checkout, branch name, tag, CI result, or
operator urgency.

## Context router

Load only the context needed for the task:

| Task condition | Load |
| --- | --- |
| Current release, runtime, deploy, migration, feature rollout, or milestone state matters | [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) |
| Complex multi-phase work, TaskIntent/control-plane evidence, release/security-sensitive work, or evidence lineage is required | [`docs/TASK_LIFECYCLE.md`](docs/TASK_LIFECYCLE.md) and `scripts/prepare_task.py` |
| Serious incident or recurring safety/data-integrity failure | [`docs/FAILURE_CAPTURE_LOOP.md`](docs/FAILURE_CAPTURE_LOOP.md) |
| Production deploy, readiness, migration, backup/restore, or rollback | [`skills/deploy/SKILL.md`](skills/deploy/SKILL.md) |
| Memory OS, authoritative SQLite memory, Qdrant, restore, epoch, or convergence work | [`skills/memory/SKILL.md`](skills/memory/SKILL.md) |
| Telegram runtime, polling/webhook, routing, connectivity, or smoke work | [`skills/telegram/SKILL.md`](skills/telegram/SKILL.md) |
| Normal bounded code or documentation change | Relevant source, tests, and directly related documentation only |

`docs/CURRENT_STATE.md` is authoritative when current state is relevant; it is
not required for a typo, formatting-only change, isolated test, narrow refactor,
or documentation work unrelated to runtime state. `prepare_task.py` is required
for the complex/lifecycle-sensitive cases in the table and for
`INTENT_CONTROL_PLANE=REQUIRED`; it is optional for a small, bounded change that
does not need its evidence package.

For complex prompts, use the machine-checkable `ai_engineering.PromptSpec`
contract and its relevant deterministic validation/evals. Simple task prompts
should state goal, scope, authority, completion boundary, task-specific
constraints, and output contract; they should link to canonical policy rather
than copy it.

## Validation principle

Inspect the changed surface, run focused validation, add related regressions
when justified, repair task-caused failures, rerun affected checks, and run
repository-level validation when applicable. Always run `git diff --check`
before delivery. `scripts/agent_check.sh` is expected for code-changing work
when applicable; it is not a substitute for a relevant focused test.

## Completion and reporting

Report the exact base, branch, worktree, changed files, checks actually run,
their outcomes, remaining risks, and next authorized action. Never claim an
unrun check, a mutation, or a deployment. A Draft-PR boundary forbids merge;
a merge boundary does not imply build, deployment, or smoke authority.

## Instruction precedence

Apply, in order: data and production safety; the current task's explicit
authority and stop boundary; applicable repository/domain contracts; these
instructions; then local engineering judgment.
